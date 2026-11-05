# test_budget.py - 예산 계산(calculate_budget) 단위 테스트 (docs/SPEC.md 2.3)
# 실행: python -m unittest discover tests
# 환율은 고정값(1 JPY = 9 KRW)으로 바꿔 네트워크 없이 정확한 값을 검사한다.
import unittest

from src import config
from src.tools import BudgetError, BudgetItem, compute_budget, judge_budget, render_budget, room_count


def fixed_rate(from_currency: str, to_currency: str) -> tuple[float, int]:
    """테스트용 고정 환율: 1 JPY = 9 KRW."""
    return {("JPY", "KRW"): 9.0}[(from_currency, to_currency)], 0


def item(name, lo, hi, unit, currency="KRW", basis="user_input", day=None, category="other") -> BudgetItem:
    """테스트용 비용 항목을 만든다."""
    return BudgetItem(name=name, min_amount=lo, max_amount=hi, currency=currency, unit=unit, basis=basis,
                      day=day, category=category)


class TestBudget(unittest.TestCase):
    def test_room_count_rounds_up(self):
        """객실 수 = 인원 ÷ 2 올림."""
        self.assertEqual([room_count(n) for n in (1, 2, 3, 4, 5)], [1, 1, 2, 2, 3])

    def test_units_and_totals(self):
        """5개 단위별 곱셈과 일행 총액·1인당 금액. 3명, 2박3일, 객실 2개."""
        items = [
            item("항공 왕복", 300_000, 400_000, "per_person"),           # ×3명       = 900,000~1,200,000
            item("식비", 30_000, 50_000, "per_person_per_day"),         # ×3명×3일   = 270,000~450,000
            item("숙박", 100_000, 150_000, "per_room_per_night"),       # ×2실×2박   = 400,000~600,000
            item("투어", 60_000, 60_000, "group"),                      # ×1         = 60,000
            item("렌터카", 50_000, 50_000, "group_per_day"),            # ×3일       = 150,000
        ]
        r = compute_budget(items, people=3, nights=2, rate_fn=fixed_rate)
        self.assertEqual([row["multiplier"] for row in r["rows"]], [3, 9, 4, 1, 3])
        self.assertEqual(r["group_total"], (1_780_000, 2_460_000))
        self.assertAlmostEqual(r["per_person"][0], 1_780_000 / 3)
        self.assertEqual(r["per_person"][1], 820_000)

    def test_currency_conversion(self):
        """JPY 금액을 KRW로 환산해 합산한다. 2명 1박: 숙박 10,000~12,000 JPY × 1실 × 1박."""
        r = compute_budget([item("숙박", 10_000, 12_000, "per_room_per_night", currency="JPY")],
                           people=2, nights=1, rate_fn=fixed_rate)
        self.assertEqual(r["group_total"], (90_000, 108_000))
        self.assertEqual(r["rates"], {"JPY": 9.0})

    def test_daily_allocation_sums_to_total(self):
        """일자 배분: 1일당 → 매일, 1박당 → 1~박수 일차, day 지정 → 그날, 1회성 → 여행 전체. 합은 총액과 같다."""
        items = [
            item("항공 왕복", 500_000, 500_000, "per_person"),
            item("식비", 40_000, 40_000, "per_person_per_day"),
            item("숙박", 120_000, 120_000, "per_room_per_night"),
            item("유니버설 입장권", 100_000, 100_000, "per_person", basis="source", day=2),
        ]
        r = compute_budget(items, people=2, nights=3, rate_fn=fixed_rate)
        self.assertEqual(r["daily"][1], (200_000, 200_000))   # 식비 80,000 + 숙박 120,000
        self.assertEqual(r["daily"][2], (400_000, 400_000))   # 식비 + 숙박 + 입장권 200,000
        self.assertEqual(r["daily"][4], (80_000, 80_000))     # 마지막 날은 숙박 없음
        self.assertEqual(r["whole_trip"], (1_000_000, 1_000_000))
        daily_sum = sum(lo for lo, _ in r["daily"].values()) + r["whole_trip"][0]
        self.assertEqual(daily_sum, r["group_total"][0])

    def test_budget_judgement_boundaries(self):
        """최대값 기준: ≤예산 안 / ≤예산×1.1 근접 / 그 이상 초과."""
        budget = 1_000_000
        self.assertEqual(judge_budget(1_000_000, budget), ("within", 0))
        self.assertEqual(judge_budget(budget * (1 + config.BUDGET_TOLERANCE), budget)[0], "near")
        self.assertEqual(judge_budget(1_100_001, budget), ("over", 100_001))

    def test_per_person_budget_basis(self):
        """1인 예산이면 1인당 최대값으로 판정한다. 2명, 총 2,400,000 → 1인 1,200,000 > 1,100,000 → 초과 200,000."""
        items = [item("항공 왕복", 600_000, 600_000, "per_person"),
                 item("숙박", 300_000, 300_000, "per_room_per_night")]
        r = compute_budget(items, people=2, nights=4, budget=1_000_000, budget_basis="per_person", rate_fn=fixed_rate)
        self.assertEqual(r["group_total"], (2_400_000, 2_400_000))
        self.assertEqual(r["judgement"], {"basis": "per_person", "budget": 1_000_000, "verdict": "over", "diff": 200_000})
        text = render_budget(r)
        self.assertIn("예산 초과", text)
        self.assertIn("200,000 KRW 초과", text)

    def test_estimate_only_for_big_items(self):
        """출처 없는 LLM 추정치는 큰 항목(항공·숙박·식비·현지 교통)만 계산하고, 입장료 추정치는 제외한다."""
        items = [item("현지 교통", 10_000, 20_000, "per_person_per_day", basis="llm_estimate", category="local_transport"),
                 item("입장료", 30_000, 30_000, "per_person", basis="llm_estimate", category="admission")]
        r = compute_budget(items, people=1, nights=1, budget=100_000, rate_fn=fixed_rate)
        self.assertEqual(r["group_total"], (20_000, 40_000))
        self.assertEqual(r["excluded"], ["입장료"])
        text = render_budget(r)
        self.assertIn("LLM 추정치(출처 없음, 사용자 확인 필요)", text)
        self.assertIn("60,000 KRW 여유", text)

    def test_invalid_inputs(self):
        """잘못된 입력은 BudgetError."""
        with self.assertRaises(BudgetError):
            compute_budget([item("식비", 50_000, 30_000, "per_person_per_day")], people=1, nights=1, rate_fn=fixed_rate)
        with self.assertRaises(BudgetError):
            compute_budget([], people=0, nights=1, rate_fn=fixed_rate)
        with self.assertRaises(BudgetError):
            compute_budget([item("입장권", 1, 1, "per_person", day=5)], people=1, nights=2, rate_fn=fixed_rate)


if __name__ == "__main__":
    unittest.main()
