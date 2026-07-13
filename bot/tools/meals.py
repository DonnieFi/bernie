"""Meal + grocery tool handlers."""
from __future__ import annotations

from collections import defaultdict

from tools import ROLE_ALL, tool
import db_writes


@tool(
    name="get_meals",
    description="Get meal plan for a date range.",
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
            "end_date":   {"type": "string", "description": "End date YYYY-MM-DD"},
        },
        "required": ["start_date", "end_date"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_meals(args: dict, ctx) -> str:
    db = ctx.services.db
    meals = await db.get_meals(args["start_date"], args["end_date"])
    if not meals:
        return f"No meals planned between {args['start_date']} and {args['end_date']}"
    lines = []
    for m in meals:
        line = f"• {m['date']} [{m['meal_type']}]: {m['dish']}"
        if m.get("notes"):
            line += f" ({m['notes']})"
        lines.append(line)
    return "\n".join(lines)


@tool(
    name="set_meal",
    description="Add or update a meal in the plan.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "date":      {"type": "string", "description": "Date YYYY-MM-DD"},
            "meal_type": {"type": "string", "description": "dinner, lunch, or breakfast"},
            "dish":      {"type": "string", "description": "What's for dinner?"},
            "notes":     {"type": "string", "description": "Optional notes"},
        },
        "required": ["date", "meal_type", "dish"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_set_meal(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called set_meal({args})]"
    db = ctx.services.db
    await db_writes.routed("set_meal", args["date"], args["meal_type"].lower(), args["dish"], args.get("notes", ""))
    return f"Meal set for {args['date']} ({args['meal_type']}): {args['dish']}"


@tool(
    name="delete_meal",
    description="Remove a meal from the plan.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "date":      {"type": "string", "description": "Date YYYY-MM-DD"},
            "meal_type": {"type": "string", "description": "dinner, lunch, or breakfast"},
        },
        "required": ["date", "meal_type"],
    },
    role_required=ROLE_ALL,
    tier=3,
)
async def handle_delete_meal(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called delete_meal({args})]"
    db = ctx.services.db
    await db_writes.routed("delete_meal", args["date"], args["meal_type"].lower())
    return f"Deleted meal for {args['date']} ({args['meal_type']})"


@tool(
    name="search_food_ideas",
    description="Search Spoonacular for meal ideas and recipes by ingredient or dish name.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Ingredient or dish name"},
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_search_food_ideas(args: dict, ctx) -> str:
    import food_service
    meals = await food_service.search_meals(args["query"], ctx.services.session)
    if not meals:
        return f"No meal ideas found for '{args['query']}'"
    return "Here are some ideas:\n" + "\n\n".join(food_service.format_meal(m) for m in meals)


@tool(
    name="add_grocery_item",
    description="Add an item to the categorized grocery list.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "item":     {"type": "string", "description": "Item to add (e.g. 'Apples')"},
            "category": {"type": "string", "description": "Category (e.g. Produce, Dairy)"},
        },
        "required": ["item", "category"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_add_grocery_item(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called add_grocery_item({args})]"
    db = ctx.services.db
    await db_writes.routed("add_grocery", args["item"], args["category"])
    return f"Added {args['item']} to {args['category']} list."


@tool(
    name="remove_grocery_item",
    description="Remove an item from the grocery list.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "Exact item name to remove"},
        },
        "required": ["item"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_remove_grocery_item(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called remove_grocery_item({args})]"
    db = ctx.services.db
    await db_writes.routed("remove_grocery", args["item"])
    return f"Removed {args['item']} from the grocery list."


@tool(
    name="get_grocery_list",
    description="Get the current shared grocery list.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_grocery_list(args: dict, ctx) -> str:
    db = ctx.services.db
    items = await db.get_groceries()
    if not items:
        return "The grocery list is empty."
    cats: dict = defaultdict(list)
    for i in items:
        cats[i["category"]].append(i["item"])
    res = "🛒 **Current Grocery List:**"
    for cat, list_items in sorted(cats.items()):
        res += f"\n\n**{cat}**\n• " + "\n• ".join(list_items)
    return res
