from fetch_data import fetch_bom_data, build_bom_tree, calculate_max_units
import json

def store_craftable_non_craftable_goods(connection, craftable_goods, non_craftable_goods):
    with connection.cursor() as cursor:
        for fg_code, max_units in craftable_goods:
            try:
                max_units_int = int(max_units)  # Ensure max_units is an integer
                cursor.execute("""
                INSERT INTO crafted_goods (bom_number, "On_hand_Qty", is_active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (bom_number) DO UPDATE SET "On_hand_Qty" = EXCLUDED."On_hand_Qty", is_active = EXCLUDED.is_active;
                """, (fg_code, max_units_int))
            except ValueError as e:
                print(f"Error converting max_units to int for {fg_code}: {max_units} - {e}")
                continue

        for fg_code, missing_items in non_craftable_goods:
            cursor.execute("""
            INSERT INTO non_craftable_goods (bom_number, "On_hand_Qty", is_active, missing_items)
            VALUES (%s, 0, FALSE, %s)
            ON CONFLICT (bom_number) DO UPDATE SET "On_hand_Qty" = EXCLUDED."On_hand_Qty", is_active = EXCLUDED.is_active, missing_items = EXCLUDED.missing_items;
            """, (fg_code, json.dumps(missing_items)))

        connection.commit()

def assemble_finished_good(connection, finished_good_code, quantity, confirm=False):
    bom_data = fetch_bom_data(connection, finished_good_code)
    if not bom_data:
        return {"success": False, "message": f"No BOM data found for {finished_good_code}."}

    item_data, tree = build_bom_tree(bom_data, finished_good_code)
    max_units, shortages = calculate_max_units(tree, item_data, finished_good_code, quantity)

    if max_units < quantity:
        return {"success": False, "message": f"Cannot assemble {quantity}. Max craftable: {max_units}.", "shortages": shortages}

    updates = [(max(0, float(item_data[item_code]["On_hand_Qty"]) - quantity * item_data[item_code]["Extended_Quantity"]), item_code)
               for item_code in item_data]

    if not confirm:
        return {"success": False, "message": "Confirmation required.", "updates": updates}

    with connection.cursor() as cursor:
        for new_qty, item_code in updates:
            cursor.execute('UPDATE admin_parts_duplicate SET "On_hand_Qty" = %s WHERE "Item_code" = %s;', (new_qty, item_code))
        connection.commit()

    return {"success": True, "message": f"{quantity} units of {finished_good_code} assembled."}
