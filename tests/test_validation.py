import unittest
from datetime import date
from decimal import Decimal

from floorsheet_downloader import (
    FloorsheetError,
    ShortPage,
    Transaction,
    classify_quality,
    extract_daily_totals,
    parse_market_date,
    validate_identifiers,
)


class FakeElement:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_text(self, separator: str = " ", strip: bool = True) -> str:
        return self.text.strip() if strip else self.text


class FakeSoup:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def find(self, *args, **kwargs):
        value = self.values.get(kwargs.get("id"))
        return FakeElement(value) if value is not None else None


def transaction(number: int = 1) -> Transaction:
    return Transaction(
        transaction_no=f"202608210000{number:04d}",
        symbol="TEST",
        buyer=1,
        seller=2,
        quantity=1,
        rate=Decimal("1"),
        amount=Decimal("1"),
        source_page=1,
    )


class ValidationTests(unittest.TestCase):
    def test_market_date(self) -> None:
        self.assertEqual(
            parse_market_date("As of 2026/08/21 03:00:00"), date(2026, 8, 21)
        )

    def test_displayed_totals(self) -> None:
        soup = FakeSoup(
            {
                "ctl00_ContentPlaceHolder1_totalQty": "10,022,765",
                "ctl00_ContentPlaceHolder1_totalAmount": "Rs 4,167,618,882.32",
            }
        )
        quantity, amount = extract_daily_totals(soup)
        self.assertEqual(quantity, 10_022_765)
        self.assertEqual(amount, Decimal("4167618882.32"))

    def test_complete(self) -> None:
        rows = [transaction(1)]
        status, _ = classify_quality(1, 1, Decimal("1"), rows, [])
        self.assertEqual(status, "COMPLETE")

    def test_minor_gap_at_limit(self) -> None:
        rows = [transaction(number) for number in range(1, 1000)]
        short_page = ShortPage(2, 501, 1000, 500, 499)
        status, _ = classify_quality(
            1000, 1000, Decimal("1000"), rows, [short_page]
        )
        self.assertEqual(status, "MINOR_GAP")

    def test_reject_above_limit(self) -> None:
        rows = [transaction(number) for number in range(1, 999)]
        short_page = ShortPage(2, 501, 1000, 500, 498)
        status, _ = classify_quality(
            1000, 1000, Decimal("1000"), rows, [short_page]
        )
        self.assertEqual(status, "REJECT")

    def test_known_2026_08_21_gap(self) -> None:
        empty_row = Transaction(
            transaction_no="2026082100000001",
            symbol="TEST",
            buyer=1,
            seller=2,
            quantity=0,
            rate=Decimal("0"),
            amount=Decimal("0"),
            source_page=1,
        )
        rows = [empty_row] * 59_756
        rows[0] = Transaction(
            transaction_no=rows[0].transaction_no,
            symbol="TEST",
            buyer=1,
            seller=2,
            quantity=10_017_660,
            rate=Decimal("0"),
            amount=Decimal("4167352802.32"),
            source_page=1,
        )
        short_page = ShortPage(53, 26001, 26500, 500, 499)
        status, _ = classify_quality(
            59_805,
            10_022_765,
            Decimal("4167618882.32"),
            rows,
            [short_page],
        )
        self.assertEqual(status, "MINOR_GAP")

    def test_duplicate_identifiers_are_rejected(self) -> None:
        with self.assertRaisesRegex(FloorsheetError, "duplicate"):
            validate_identifiers(date(2026, 8, 21), [transaction(1), transaction(1)])


if __name__ == "__main__":
    unittest.main()
