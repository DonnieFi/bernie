"""family-bot-fqa9.1: meals and grocery HTTP API parity."""
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, patch

from fastapi import HTTPException

from api.routes.meals import MealWrite, GroceryWrite, _range, build_meals_router


def _endpoint(router, method, path):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not found")


class TestMealsApiRange(unittest.TestCase):
    def test_rejects_reverse_range(self):
        with self.assertRaises(HTTPException):
            _range("2026-07-23", "2026-07-22")


class TestMealsApiWrites(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = AsyncMock()
        self.db.get_meals.return_value = [
            {"date": "2026-07-22", "meal_type": "dinner", "dish": "Tacos", "notes": ""},
        ]
        self.db.get_groceries.return_value = [{"item": "Milk", "category": "Dairy"}]
        self.router = build_meals_router(SimpleNamespace(db=self.db))
        self.parent = SimpleNamespace(id="person:red", role="parents")
        self.kid = SimpleNamespace(id="person:child1", role="member")

    async def test_get_meals_filters_type(self):
        meals = await _endpoint(self.router, "GET", "/api/meals")(
            start="2026-07-22", end="2026-07-22", meal_type="dinner"
        )
        self.assertEqual([meal["dish"] for meal in meals], ["Tacos"])

    async def test_put_meal_requires_parent(self):
        with self.assertRaises(HTTPException) as ctx:
            await _endpoint(self.router, "PUT", "/api/meals")(
                MealWrite(date="2026-07-22", meal_type="dinner", dish="Pasta"),
                user=self.kid,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_put_meal_routes_write(self):
        with patch("api.routes.meals.db_writes.routed", new_callable=AsyncMock) as routed:
            with patch("api.routes.meals._mirror_dinner", new_callable=AsyncMock, return_value=None):
                out = await _endpoint(self.router, "PUT", "/api/meals")(
                    MealWrite(date="2026-07-22", meal_type="dinner", dish="Pasta", notes="easy"),
                    user=self.parent,
                )
        routed.assert_awaited_once()
        self.assertEqual(routed.await_args.args[:4], ("set_meal", "2026-07-22", "dinner", "Pasta"))
        self.assertTrue(out["ok"])

    async def test_delete_meal_routes_write(self):
        self.db.get_meals.return_value[0]["gcal_event_id"] = "gcal-123"
        with patch("api.routes.meals.db_writes.routed", new_callable=AsyncMock) as routed, \
             patch("api.routes.meals._delete_mirrored_dinner", new_callable=AsyncMock) as mirrored:
            out = await _endpoint(self.router, "DELETE", "/api/meals")(
                date="2026-07-22", meal_type="dinner", user=self.parent,
            )
        mirrored.assert_awaited_once_with(ANY, "gcal-123")
        routed.assert_awaited_once_with("delete_meal", "2026-07-22", "dinner")
        self.assertTrue(out["ok"])

    async def test_grocery_crud(self):
        with patch("api.routes.meals.db_writes.routed", new_callable=AsyncMock) as routed:
            added = await _endpoint(self.router, "POST", "/api/groceries")(
                GroceryWrite(item="Eggs", category="Dairy"), user=self.kid,
            )
            removed = await _endpoint(self.router, "DELETE", "/api/groceries")(
                item="Eggs", user=self.kid,
            )
        self.assertEqual(routed.await_args_list[0].args, ("add_grocery", "Eggs", "Dairy"))
        self.assertEqual(routed.await_args_list[1].args, ("remove_grocery", "Eggs"))
        self.assertTrue(added["ok"] and removed["ok"])

    async def test_get_groceries(self):
        items = await _endpoint(self.router, "GET", "/api/groceries")()
        self.assertEqual(items[0]["item"], "Milk")

    async def test_suggest_groceries_from_dinners(self):
        self.db.get_meals.return_value = [
            {"date": "2026-07-22", "meal_type": "dinner", "dish": "Tacos", "notes": ""},
        ]
        data = await _endpoint(self.router, "POST", "/api/meals/suggest-groceries")(
            start="2026-07-22", end="2026-07-22", meal_type="dinner"
        )
        self.assertTrue(any(row["item"] == "tortillas" for row in data["suggestions"]))


if __name__ == "__main__":
    unittest.main()
