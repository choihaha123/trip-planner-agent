# test_dates.py - 날짜 계산(resolve_dates) 단위 테스트 (docs/SPEC.md 2.4)
# 실행: python -m unittest discover tests
# 오늘을 고정(2026-11-05 목요일)해 항상 같은 결과로 검사한다. 한 주는 월~일.
import unittest
from datetime import date

from src.tools import DateError, compute_dates, render_dates

TODAY = date(2026, 11, 5)  # 목요일. 이번 주 = 11/2(월)~11/8(일), 다음 주 = 11/9(월)~11/15(일)


def d(month: int, day: int, year: int = 2026) -> date:
    """테스트용 날짜를 만든다."""
    return date(year, month, day)


class TestDates(unittest.TestCase):
    def test_next_week_weekday(self):
        """다음 주 토요일 = 다음 주 월~일 안의 토요일."""
        self.assertEqual(compute_dates(TODAY, weeks_from_now=1, weekday="sat")["dates"], [d(11, 14)])
        self.assertEqual(compute_dates(TODAY, weeks_from_now=1, weekday="mon")["dates"], [d(11, 9)])

    def test_whole_week(self):
        """weekday를 비우면 그 주 월~일 전체."""
        dates = compute_dates(TODAY, weeks_from_now=1)["dates"]
        self.assertEqual((dates[0], dates[-1], len(dates)), (d(11, 9), d(11, 15), 7))

    def test_weekend(self):
        """주말 = 그 주 토~일. 이번 주말 중 지난 날은 빼고, 일요일이면 오늘만."""
        self.assertEqual(compute_dates(TODAY, weeks_from_now=0, weekday="weekend")["dates"], [d(11, 7), d(11, 8)])
        self.assertEqual(compute_dates(TODAY, weeks_from_now=1, weekday="weekend")["dates"], [d(11, 14), d(11, 15)])
        self.assertEqual(compute_dates(d(11, 8), weeks_from_now=0, weekday="weekend")["dates"], [d(11, 8)])

    def test_days_from_today(self):
        """모레 = 오늘 + 2일."""
        self.assertEqual(compute_dates(TODAY, days_from_today=2)["dates"], [d(11, 7)])

    def test_absolute_date(self):
        """연도 없는 MM-DD는 오늘 이후 가장 가까운 날짜, 지났으면 다음 해."""
        self.assertEqual(compute_dates(TODAY, date_text="12-24")["dates"], [d(12, 24)])
        self.assertEqual(compute_dates(TODAY, date_text="01-10")["dates"], [d(1, 10, 2027)])
        self.assertEqual(compute_dates(TODAY, date_text="2026-11-14")["dates"], [d(11, 14)])

    def test_trip_period(self):
        """다음 주 토요일 출발 3박 → 11/17 귀국, 4일."""
        trip = compute_dates(TODAY, weeks_from_now=1, weekday="sat", nights=3)["trip"]
        self.assertEqual((trip["start"], trip["end"]), (d(11, 14), d(11, 17)))
        self.assertEqual(trip["days"], [d(11, 14), d(11, 15), d(11, 16), d(11, 17)])
        self.assertIn("3박4일", render_dates(compute_dates(TODAY, weeks_from_now=1, weekday="sat", nights=3), TODAY))

    def test_year_boundary(self):
        """연말에서 다음 주 월요일은 다음 해로 넘어간다. 2026-12-30(수) → 2027-01-04(월)."""
        self.assertEqual(compute_dates(d(12, 30), weeks_from_now=1, weekday="mon")["dates"], [d(1, 4, 2027)])

    def test_past_date_flagged(self):
        """이번 주 월요일(11/2)은 이미 지난 날짜로 표시한다."""
        result = compute_dates(TODAY, weeks_from_now=0, weekday="mon")
        self.assertEqual(result["past"], [d(11, 2)])
        self.assertIn("이미 지난 날짜", render_dates(result, TODAY))

    def test_invalid_inputs(self):
        """기준 표현이 없거나 둘 이상, 잘못된 요일·날짜·박 수는 DateError."""
        bad_calls = [
            {},                                             # 기준 표현 없음
            {"weeks_from_now": 1, "days_from_today": 2},    # 기준 표현 둘
            {"weekday": "sat", "days_from_today": 1},       # weekday 단독 사용
            {"weeks_from_now": 1, "weekday": "토"},          # 잘못된 요일 코드
            {"date_text": "2026-02-30"},                    # 없는 날짜
            {"days_from_today": 1, "nights": -1},           # 음수 박 수
        ]
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(DateError):
                compute_dates(TODAY, **kwargs)

    def test_fix_weekdays(self):
        """답변 속 '날짜(요일)'의 요일이 달력과 다르면 요일만 고친다. 맞으면 그대로, 형식은 유지한다. (S1-1)"""
        from src.tools import fix_weekdays
        text, fixed = fix_weekdays("다음 주 토요일 11/15(토), 11월 14일 토요일, 2026-11-14(토), 11월 15일(토요일)", TODAY)
        self.assertEqual(text, "다음 주 토요일 11/15(일), 11월 14일 토요일, 2026-11-14(토), 11월 15일(일요일)")
        self.assertEqual(len(fixed), 2)
        self.assertEqual(fix_weekdays("1월 3일(토)", TODAY)[0], "1월 3일(일)")       # 지난 1월 → 내년(2027-01-03 일)
        self.assertEqual(fix_weekdays("2/30(월) 같은 없는 날짜", TODAY)[0], "2/30(월) 같은 없는 날짜")

    def test_relative_date_expressions(self):
        """상대 날짜 표현(다음 주·담주·토욜·모레·N일 뒤 등)은 날짜 계산 강제 대상이고, 오늘·내일은 아니다. (S1-1)"""
        from src.tools import RELATIVE_DATE
        for text in ("담주 토욜 오사카", "다음 주말", "이번 주 금요일", "모레 날씨", "3일 뒤", "1주일 후", "다음 달 도쿄"):
            self.assertTrue(RELATIVE_DATE.search(text), text)
        for text in ("내일 날씨", "오늘 환율", "오사카 맛집"):
            self.assertIsNone(RELATIVE_DATE.search(text), text)

    def test_leap_day_without_year(self):
        """연도 없는 02-29는 오늘 이후 가장 가까운 윤년의 2월 29일이다(평년에도 오류 없음). 연도가 있으면 그대로 검사한다. (S1-4)"""
        self.assertEqual(compute_dates(TODAY, date_text="02-29")["dates"], [d(2, 29, 2028)])
        self.assertEqual(compute_dates(d(2, 29, 2028), date_text="02-29")["dates"], [d(2, 29, 2028)])   # 그날 당일
        self.assertEqual(compute_dates(d(3, 1, 2028), date_text="02-29")["dates"], [d(2, 29, 2032)])    # 지났으면 다음 윤년
        with self.assertRaises(DateError):
            compute_dates(TODAY, date_text="2027-02-29")


if __name__ == "__main__":
    unittest.main()
