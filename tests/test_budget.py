# test_budget.py - 예산 계산(calculate_budget) 단위 테스트 (docs/SPEC.md 2.3)
# 실행: python -m unittest discover tests
# 환율은 고정값(1 JPY = 9 KRW)으로 바꿔 네트워크 없이 정확한 값을 검사한다.
import unittest

from src import config
from src.tools import (AdjustmentChange, BudgetAdjustment, BudgetError, BudgetItem, compute_budget,
                       evaluate_adjustments, judge_budget, render_budget, room_count)


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

    def test_lodging_must_be_per_room_per_night(self):
        """숙박은 객실 1박 단가로만 받는다. 1박 값을 1회 비용으로 넣으면 박 수만큼 빠지므로 거부한다(R1-4).
        분류가 lodging이거나, 분류가 other인데 이름이 숙박류일 때 검사하고, 교통으로 분류한 '호텔 셔틀'과 당일 일정은 검사하지 않는다."""
        for lodging in (item("숙박 (2박)", 150_000, 180_000, "per_person", category="lodging"),
                        item("호텔", 150_000, 180_000, "per_person"),
                        item("료칸", 300_000, 300_000, "group")):
            with self.assertRaises(BudgetError) as e:
                compute_budget([lodging], people=1, nights=2, rate_fn=fixed_rate)
            self.assertIn("per_room_per_night", str(e.exception))
        ok = compute_budget([item("숙박", 150_000, 180_000, "per_room_per_night", category="lodging")],
                            people=1, nights=2, rate_fn=fixed_rate)
        self.assertEqual(ok["group_total"], (300_000, 360_000))                   # 1박 단가 × 2박
        compute_budget([item("호텔 셔틀버스", 1_000, 1_000, "per_person", category="local_transport")],
                       people=1, nights=2, rate_fn=fixed_rate)
        compute_budget([item("숙박", 100_000, 100_000, "group", category="lodging")], people=1, nights=0, rate_fn=fixed_rate)

    def test_room_basis_shown_only_for_groups(self):
        """'(2인 1실 기준)'은 2명 이상일 때만 보인다(혼자인데 2인 1실이라 하던 표시)."""
        lodging = [item("숙박", 100_000, 100_000, "per_room_per_night", category="lodging")]
        solo = render_budget(compute_budget(lodging, people=1, nights=1, rate_fn=fixed_rate), for_user=True)
        pair = render_budget(compute_budget(lodging, people=2, nights=1, rate_fn=fixed_rate), for_user=True)
        self.assertNotIn("1실 기준", solo)
        self.assertIn(f"객실 1개({config.PEOPLE_PER_ROOM}인 1실 기준)", pair)



class TestBudgetAdjustments(unittest.TestCase):
    """예산 조정안은 프로그램이 다시 계산한 절감액·적용 후 판정으로만 보여 준다. (TASKS Phase 8 S1-2)
    2명·2박3일·객실 1개, 예산 총액 900,000: 항공 600,000 + 숙박 360,000~520,000 = 최대 1,120,000 (220,000 초과, 24%)."""

    def setUp(self):
        """초과 폭이 15%를 넘어 조정안이 꼭 필요한 예산."""
        self.items = [item("항공 왕복", 300_000, 300_000, "per_person", category="flight"),
                      item("숙박", 180_000, 260_000, "per_room_per_night", category="lodging")]
        self.base = compute_budget(self.items, 2, 2, budget=900_000)

    def evaluate(self, adjustments):
        """조정안을 다시 계산한다."""
        return evaluate_adjustments(adjustments, self.items, 2, 2, 900_000, "total", "KRW", self.base)

    def test_claim_replaced_by_recalculated_verdict(self):
        """사례 1: 숙박 18~26만 → 18~20만이면 '예산 내 가능'이라고 써도, 실제로는 120,000만 줄어 초과가 남는다고 붙인다.
        판정 단언은 설명에서 지우고, 바꾼 금액에는 출처 없음 표시를 붙인다."""
        lines = self.evaluate([BudgetAdjustment(description="숙박을 18~20만원대로 조정하면 예산 내 가능",
                                                changes=[AdjustmentChange(item="숙박", new_min_amount=180_000,
                                                                          new_max_amount=200_000)])])
        description, effect = lines[0]
        self.assertEqual(description, "숙박 금액 조정")
        self.assertIn(f"숙박 180,000~260,000 KRW → 180,000~200,000 KRW ({config.NO_SOURCE_LABEL})", effect)
        self.assertIn("일행 총액 최대 120,000 KRW 절감(1인 60,000 KRW)", effect)
        self.assertIn("적용 후 판정: 예산 초과 (최대 100,000 KRW 초과)", effect)
        self.assertIn("적용 후 일행 총액 960,000~1,000,000 KRW", effect)

    def test_combined_line(self):
        """사례 2: 조정안이 둘 이상이면 '모두 적용하면' 줄로 합친 효과를 계산한다(합쳐도 초과면 초과)."""
        lines = self.evaluate([
            BudgetAdjustment(description="숙박을 비즈니스호텔로",
                             changes=[AdjustmentChange(item="숙박", new_min_amount=150_000, new_max_amount=200_000)]),
            BudgetAdjustment(description="저가 항공으로", changes=[AdjustmentChange(item="항공 왕복", new_min_amount=250_000,
                                                                                new_max_amount=260_000)]),
            BudgetAdjustment(description="걷기 위주 동선")])
        self.assertEqual(lines[2], ("걷기 위주 동선", "금액 효과는 계산하지 않음"))
        self.assertEqual(lines[3][0], "모두 적용하면")
        self.assertIn("일행 총액 최대 200,000 KRW 절감", lines[3][1])              # 120,000 + 80,000
        self.assertIn("적용 후 판정: 예산 근접", lines[3][1])                       # 920,000 ≤ 900,000×1.1

    def test_invalid_adjustments(self):
        """없는 항목 이름이나, 조정안이 꼭 필요한데 금액이 바뀌는 조정안이 없으면 오류로 돌려보낸다."""
        with self.assertRaises(BudgetError):
            self.evaluate([BudgetAdjustment(description="호텔 바꾸기",
                                            changes=[AdjustmentChange(item="호텔", new_min_amount=1, new_max_amount=1)])])
        with self.assertRaises(BudgetError):
            self.evaluate([BudgetAdjustment(description="일정을 줄인다")])

    def test_itinerary_shows_recalculated_lines(self):
        """일정표·화면 카드의 조정안 줄은 '설명: 다시 계산한 결과'이고, 출처 없음 표시는 모델이 쓴 설명의 가격에만 붙는다."""
        from src.tools import DayPlan, ScheduleItem, adjustment_line, build_itinerary_text, itinerary_view
        lines = self.evaluate([BudgetAdjustment(description="숙박을 20만원 이하 호텔로",
                                                changes=[AdjustmentChange(item="숙박", new_min_amount=180_000,
                                                                          new_max_amount=200_000)])])
        days = [DayPlan(day=d, items=[ScheduleItem(time="오전", place="오사카성")]) for d in (1, 2, 3)]
        text = build_itinerary_text("오사카", "오사카", 2, 2, "인천", days, budget_result=self.base, adjustments=lines)
        expected = (f"- 숙박을 20만원 ({config.NO_SOURCE_LABEL}) 이하 호텔로: 숙박 180,000~260,000 KRW → "
                    f"180,000~200,000 KRW ({config.NO_SOURCE_LABEL}) → 일행 총액 최대 120,000 KRW 절감(1인 60,000 KRW), "
                    "적용 후 일행 총액 960,000~1,000,000 KRW, 적용 후 판정: 예산 초과 (최대 100,000 KRW 초과)")
        self.assertIn(expected, text)
        artifact = {"days": [d.model_dump() for d in days], "adjustment_lines": [adjustment_line(a) for a in lines],
                    "adjustments": ["숙박을 20만원 이하 호텔로"]}
        self.assertEqual(itinerary_view(artifact)["adjustments"], [expected[2:]])


if __name__ == "__main__":
    unittest.main()
