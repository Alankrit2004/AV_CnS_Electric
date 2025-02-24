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

def build_bom_tree(bom_data, finished_good_code):
    """
    Builds a hierarchical BOM tree based on Item Level.
    Ensures each item is assigned to the correct parent.
    """
    item_data = {row["Item_code"]: row for row in bom_data}  # Store item details
    tree = defaultdict(list)  # Initialize tree structure
    parent_map = {}  # Maps items to their correct parent

    # Sort by Item Level (ensures parents are processed before children)
    sorted_bom = sorted(bom_data, key=lambda x: x["Item_Level"])  

    for row in sorted_bom:
        item_code = row["Item_code"]
        parent_code = row["Code"]  # Always the finished good code

        if row["Item_Level"] == 1:
            # Direct child of the finished good (root node)
            tree[finished_good_code].append(item_code)
        else:
            # Assign the correct parent from parent_map
            if parent_code in tree:
                tree[parent_code].append(item_code)
            else:
                # If the parent is not yet in the tree, store it in parent_map
                parent_map[item_code] = parent_code

    return item_data, tree

def calculate_max_units(tree, item_data, finished_good_code, required_quantity):
    shortages = []  # Track items causing shortages

    def recursive_calculate(item_code, quantity_needed):
        if item_code not in item_data:
            shortages.append((item_code, "Unknown"))
            return 0

        item = item_data[item_code]
        on_hand_qty = float(item.get("On_hand_Qty", 0))  
        required_qty = max(1, float(item.get("Extended_Quantity", 1)))  
        item_type = item["Type"].lower()

        print(f"Processing '{item_code}' (Type: {item_type}) - Needed: {quantity_needed}, Available: {on_hand_qty}")

        # ✅ If enough stock is available, return the max craftable units & STOP traversing
        if on_hand_qty >= quantity_needed:
            max_units = on_hand_qty // required_qty
            print(f" '{item_code}' has enough stock ({on_hand_qty}). No need to traverse further.")
            return max_units

        # 🚨 If purchased item is missing, mark as non-craftable immediately
        if item_type == "purchased item":
            if quantity_needed > on_hand_qty:
                shortages.append((item_code, quantity_needed - on_hand_qty))
                print(f" '{item_code}' is missing! Shortage: {quantity_needed - on_hand_qty}")
                return 0
            return on_hand_qty // required_qty

        # ✅ If stock is insufficient, check child items
        if item_code in tree:
            child_units = []
            for child in tree[item_code]:
                child_needed = quantity_needed * float(item_data[child]["Extended_Quantity"])
                child_max_units = recursive_calculate(child, child_needed)

                if child_max_units == 0:
                    print(f" '{item_code}' cannot be crafted. Missing child: {child}")
                    return 0

                child_units.append(child_max_units // float(item_data[child]["Extended_Quantity"]))

            max_units_from_children = min(child_units) if child_units else 0
            print(f"🔹 '{item_code}' can be crafted up to {max_units_from_children} units based on child availability.")
            return max_units_from_children

        # 🚨 If no stock & no children, mark as missing
        shortages.append((item_code, quantity_needed - on_hand_qty))
        print(f" '{item_code}' is completely missing. Cannot proceed.")
        return 0

    max_units = recursive_calculate(finished_good_code, required_quantity)
    return max_units, shortages


# def calculate_max_units(tree, item_data, finished_good_code, required_quantity):
#     shortages = []  # Track items causing shortages

#     def recursive_calculate(item_code, quantity_needed):
#         if item_code not in item_data:
#             shortages.append((item_code, "Unknown"))
#             return 0

#         item = item_data[item_code]
#         on_hand_qty = float(item["On_hand_Qty"]) if item["On_hand_Qty"] else 0
#         required_qty = max(1, float(item["Extended_Quantity"]))  # Prevent division by zero
#         item_type = item["Type"].lower()

#         # Debugging: Log each item check
#         print(f"Processing '{item_code}' (Type: {item_type}) - Needed: {quantity_needed}, Available: {on_hand_qty}, Required per unit: {required_qty}")

#         # If a purchased item is out of stock, we can't proceed
#         if item_type == "purchased item":
#             if quantity_needed > on_hand_qty:
#                 shortages.append((item_code, quantity_needed - on_hand_qty))
#                 return 0
#             return on_hand_qty // required_qty

#         # If sufficient stock exists, return available units
#         if on_hand_qty >= quantity_needed:
#             return on_hand_qty // required_qty

#         # Traverse children if the stock is insufficient
#         if item_code in tree:
#             child_units = []
#             for child in tree[item_code]:
#                 child_quantity_needed = quantity_needed * float(item_data[child]["Extended_Quantity"])
#                 units = recursive_calculate(child, child_quantity_needed)
#                 if units == 0:
#                     return 0  # If any child is missing, crafting is not possible
#                 child_units.append(units)

#             # Debugging: Check child availability
#             print(f"Child check for {item_code}: {child_units}")

#             return min(child_units) if child_units else float("inf")

#         # Leaf node with insufficient stock
#         if quantity_needed > on_hand_qty:
#             shortages.append((item_code, quantity_needed - on_hand_qty))
#             return 0

#         return on_hand_qty // required_qty

#     max_units = recursive_calculate(finished_good_code, required_quantity)
#     return max_units, shortages