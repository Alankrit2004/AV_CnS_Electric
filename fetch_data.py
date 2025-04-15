import psycopg2
from psycopg2.extras import RealDictCursor
from collections import defaultdict
import json

def fetch_bom_data(connection, finished_good_code):
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            query = """
            WITH RECURSIVE BOM_Tree AS (
                SELECT 
                    "bom_number" AS "Code", 
                    "Item_Level", 
                    "Item_code", 
                    "Type", 
                    "On_hand_Qty", 
                    "Extended_Quantity",
                    ARRAY["bom_number"] as path
                FROM "admin_parts"
                WHERE "bom_number" = %s

                UNION ALL

                SELECT 
                    b."bom_number" AS "Code", 
                    b."Item_Level", 
                    b."Item_code", 
                    b."Type", 
                    b."On_hand_Qty", 
                    b."Extended_Quantity",
                    bt.path || b."bom_number"
                FROM "admin_parts" b
                INNER JOIN BOM_Tree bt ON b."bom_number"::text = bt."Item_code"
                WHERE NOT b."bom_number" = ANY(bt.path)
            )
            SELECT "Code", "Item_Level", "Item_code", "Type", "On_hand_Qty", "Extended_Quantity"
            FROM BOM_Tree
            """
            cursor.execute(query, (finished_good_code,))
            bom_data = cursor.fetchall()
            return bom_data if bom_data else []
    except Exception as e:
        print(f"Error fetching BOM data: {e}")
        return []

from collections import defaultdict

def build_bom_tree(bom_data, finished_good_code):
    print(f"Input BOM data for {finished_good_code}: {bom_data}")
    
    item_data = {row["Item_code"]: row for row in bom_data}
    print(f"Initial item_data: {item_data}")
    
    tree = defaultdict(list)
    parent_stack = []  
   
    if finished_good_code not in item_data:
        print(f"Creating default entry for {finished_good_code}")
        item_data[finished_good_code] = {
            "On_hand_Qty": 0,
            "Extended_Quantity": 1,  
            "Type": "finished_good",
            "Item_Level": 0
        }
        # print(f"Updated item_data with default entry: {item_data}")

    for row in bom_data:
        item_code = row["Item_code"]
        level = row["Item_Level"]

        
        while parent_stack and parent_stack[-1][1] >= level:
            parent_stack.pop()

        
        if parent_stack:
            parent = parent_stack[-1][0]
            tree[parent].append(item_code)

        
        parent_stack.append((item_code, level))

    # Add all Level 1 items as children of the finished good code
    for row in bom_data:
        if row["Item_Level"] == 1:
            tree[finished_good_code].append(row["Item_code"])

    return item_data, tree

# def calculate_max_units(tree, item_data, finished_good_code, required_quantity):
#     shortages = []  # Track items causing shortages
#     used_items = {}  # Track only required items for inventory deduction

#     def recursive_calculate(item_code, quantity_needed):
#         if item_code not in item_data:
#             shortages.append((item_code, "Unknown"))
#             return 0

#         item = item_data[item_code]
#         on_hand_qty = float(item["On_hand_Qty"]) if item["On_hand_Qty"] else 0
#         required_qty = max(1, float(item["Extended_Quantity"]))  # Prevent division by zero
#         # make_or_buy = item["Make/Buy"].lower().strip()
#         make_or_buy = item.get("Make/Buy", "make").lower().strip()


#         print(f"Processing '{item_code}' ({make_or_buy.upper()}) - Needed: {quantity_needed}, Available: {on_hand_qty}, Required per unit: {required_qty}")

#         # ✅ BUY items: must have full quantity in stock
#         if make_or_buy == "buy":
#             if quantity_needed > on_hand_qty:
#                 shortages.append((item_code, quantity_needed - on_hand_qty))
#                 return 0

#             used_items[item_code] = used_items.get(item_code, 0) + quantity_needed
#             return on_hand_qty // required_qty

#         # ✅ MAKE items: use what's in stock, and try to craft the rest
#         if on_hand_qty >= quantity_needed:
#             used_items[item_code] = used_items.get(item_code, 0) + quantity_needed
#             return on_hand_qty // required_qty

#         remaining_needed = quantity_needed - on_hand_qty
#         if on_hand_qty > 0:
#             used_items[item_code] = used_items.get(item_code, 0) + on_hand_qty

#         if item_code in tree:
#             child_units = []
#             for child in tree[item_code]:
#                 child_required = float(item_data[child]["Extended_Quantity"])
#                 total_child_needed = remaining_needed * child_required
#                 units = recursive_calculate(child, total_child_needed)
#                 if units == 0:
#                     return 0  # Fail if any child fails
#                 child_units.append(units)

#                 used_items[child] = used_items.get(child, 0) + total_child_needed

#             print(f"Child check for {item_code}: {child_units}")
#             return min(child_units) if child_units else float("inf")

#         # Leaf MAKE item but not enough stock and no children to produce more
#         shortages.append((item_code, remaining_needed))
#         return 0

#     max_units = recursive_calculate(finished_good_code, required_quantity)
#     return max_units, shortages, used_items

def calculate_max_units(tree, item_data, finished_good_code, required_quantity):
    shortages = []  # Track missing BUY items
    used_items = {}  # Track only required items for inventory deduction
    collect_all_missing_buy_items = {"flag": False}  # Becomes True after first missing BUY item

    def recursive_calculate(item_code, quantity_needed):
        if item_code not in item_data:
            shortages.append((item_code, "Unknown"))
            return 0

        item = item_data[item_code]
        on_hand_qty = float(item["On_hand_Qty"]) if item["On_hand_Qty"] else 0
        required_qty = max(1, float(item["Extended_Quantity"]))  # Prevent division by zero
        make_or_buy = item.get("Make/Buy", "make").lower().strip()

        print(f"Processing '{item_code}' ({make_or_buy.upper()}) - Needed: {quantity_needed}, Available: {on_hand_qty}, Required per unit: {required_qty}")

        # ✅ BUY items: must be fully in stock
        if make_or_buy == "buy":
            if quantity_needed > on_hand_qty:
                shortages.append((item_code, quantity_needed - on_hand_qty))
                collect_all_missing_buy_items["flag"] = True  # Trigger full traversal
                return 0
            used_items[item_code] = used_items.get(item_code, 0) + quantity_needed
            return on_hand_qty // required_qty

        # ✅ MAKE items: allow early stop if enough stock *before* we start deep traversal
        if not collect_all_missing_buy_items["flag"] and on_hand_qty >= quantity_needed:
            used_items[item_code] = used_items.get(item_code, 0) + quantity_needed
            return on_hand_qty // required_qty

        # Otherwise, calculate how many more are needed
        remaining_needed = max(0, quantity_needed - on_hand_qty)
        if on_hand_qty > 0:
            used_items[item_code] = used_items.get(item_code, 0) + min(quantity_needed, on_hand_qty)

        # Traverse children if item is in the tree
        if item_code in tree:
            child_units = []
            can_fulfill = True
            for child in tree[item_code]:
                child_required = float(item_data[child]["Extended_Quantity"])
                total_child_needed = remaining_needed * child_required
                units = recursive_calculate(child, total_child_needed)

                if units == 0:
                    can_fulfill = False
                else:
                    used_items[child] = used_items.get(child, 0) + total_child_needed
                    child_units.append(units)

            return min(child_units) if can_fulfill and child_units else 0

        # Leaf MAKE item with no children and not enough stock
        if remaining_needed > 0:
            shortages.append((item_code, remaining_needed))
        return 0

    max_units = recursive_calculate(finished_good_code, required_quantity)
    return max_units, shortages, used_items
