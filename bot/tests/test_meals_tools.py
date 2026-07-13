import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestMealsTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from tools import load_all_domains
        load_all_domains()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()

        class Ctx:
            shadow = False
            person_id = "person:red"
            group = "parents"
            config = {}
            class services:
                db = test_db
                session = None
        self.ctx = Ctx()

    async def asyncTearDown(self):
        import os
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_set_and_get_meals(self):
        from tools.meals import handle_set_meal, handle_get_meals
        # Set a meal
        set_args = {
            "date": "2026-06-01",
            "meal_type": "dinner",
            "dish": "Chicken Curry",
            "notes": "Spicy"
        }
        res_set = await handle_set_meal(set_args, self.ctx)
        self.assertIn("Meal set for 2026-06-01", res_set)

        # Get meals
        get_args = {
            "start_date": "2026-06-01",
            "end_date": "2026-06-01"
        }
        res_get = await handle_get_meals(get_args, self.ctx)
        self.assertIn("• 2026-06-01 [dinner]: Chicken Curry (Spicy)", res_get)

    async def test_delete_meal(self):
        from tools.meals import handle_set_meal, handle_get_meals, handle_delete_meal
        # Set a meal
        set_args = {
            "date": "2026-06-02",
            "meal_type": "lunch",
            "dish": "Sandwich"
        }
        await handle_set_meal(set_args, self.ctx)

        # Delete the meal
        del_args = {
            "date": "2026-06-02",
            "meal_type": "lunch"
        }
        res_del = await handle_delete_meal(del_args, self.ctx)
        self.assertIn("Deleted meal for 2026-06-02 (lunch)", res_del)

        # Get meals to confirm it is gone
        get_args = {
            "start_date": "2026-06-02",
            "end_date": "2026-06-02"
        }
        res_get = await handle_get_meals(get_args, self.ctx)
        self.assertIn("No meals planned", res_get)

    async def test_add_grocery_item_and_get_list(self):
        from tools.meals import handle_add_grocery_item, handle_get_grocery_list
        # Add item
        add_args = {
            "item": "Organic Milk",
            "category": "Dairy"
        }
        res_add = await handle_add_grocery_item(add_args, self.ctx)
        self.assertIn("Added Organic Milk to Dairy list", res_add)

        # Get grocery list
        res_list = await handle_get_grocery_list({}, self.ctx)
        self.assertIn("**Dairy**", res_list)
        self.assertIn("• Organic Milk", res_list)

    async def test_shadow_write_does_not_mutate(self):
        from tools.meals import handle_set_meal, handle_get_meals
        self.ctx.shadow = True

        # Set meal under shadow context
        set_args = {
            "date": "2026-06-03",
            "meal_type": "dinner",
            "dish": "Shadow Pasta"
        }
        res_set = await handle_set_meal(set_args, self.ctx)
        self.assertIn("[shadow: would have called set_meal", res_set)

        # Revert shadow to query and confirm nothing is stored
        self.ctx.shadow = False
        get_args = {
            "start_date": "2026-06-03",
            "end_date": "2026-06-03"
        }
        res_get = await handle_get_meals(get_args, self.ctx)
        self.assertIn("No meals planned", res_get)
