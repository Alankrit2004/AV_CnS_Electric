from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from fetch_data import fetch_bom_data
from assembly_manager import assemble_finished_good, store_craftable_non_craftable_goods
from db_connection import connect_to_database
import bcrypt
from psycopg2.extras import RealDictCursor
import psycopg2
from fetch_data import build_bom_tree, calculate_max_units
from threading import Thread
from queue import Queue
from datetime import datetime, timedelta, timezone
import threading
import pandas as pd 
import io
from io import BytesIO
import base64

app = Flask(__name__)
CORS(app)

app.config["JWT_SECRET_KEY"] = "alankrit2004"
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=24)
jwt = JWTManager(app)

def run_with_timeout(func, args=(), kwargs={}, timeout=120):
    """Run a function with timeout using threads instead of signals"""
    result_queue = Queue()
    
    def wrapper():
        try:
            result = func(*args, **kwargs)
            result_queue.put(('success', result))
        except Exception as e:
            result_queue.put(('error', str(e)))
    
    thread = Thread(target=wrapper)
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    
    if thread.is_alive():
        return 'timeout', None
    
    if not result_queue.empty():
        return result_queue.get()
    return 'error', 'Unknown error occurred'

# ✅ **User Registration API**
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500
    cursor = connection.cursor()

    try:
        cursor.execute("INSERT INTO users (username, password) VALUES (%s, %s);", 
                       (username, hashed_password.decode("utf-8")))
        connection.commit()
        return jsonify({"message": "User registered successfully"}), 201
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username already exists"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cursor.close()
        connection.close()



@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    cursor = connection.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT password FROM users WHERE username = %s;", (username,))
    user = cursor.fetchone()

    cursor.close()
    connection.close()

    if not user:
        return jsonify({"error": "User not found"}), 404

    stored_password = user["password"]
    if stored_password and bcrypt.checkpw(password.encode("utf-8"), stored_password.encode("utf-8")):
        now = datetime.now(timezone.utc)  # Ensure UTC timezone
        access_token = create_access_token(
            identity=username,
            expires_delta=timedelta(hours=24),
            additional_claims={"iat": now.timestamp(), "nbf": now.timestamp()}  # Fixes future timestamp issue
        )
        return jsonify(access_token=access_token)

    return jsonify({"error": "Invalid credentials"}), 401


# ✅ **User Logout API**
@app.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    return jsonify({"message": "Successfully logged out!"})

@app.route("/get_craftable_goods", methods=["POST"])
@jwt_required()
def get_craftable_goods():
    connection = None
    try:
        data = request.get_json()
        craft_quantity = data.get("quantity")
        specific_bom = data.get("bom_number")

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)

        if specific_bom:
            batch_codes = [specific_bom]
        else:
            cursor.execute('''
                SELECT DISTINCT "bom_number" 
                FROM "admin_parts" 
                LIMIT 10
            ''')
            batch_codes = [row['bom_number'] for row in cursor.fetchall()]
            cursor.close()

        craftable_goods = []
        non_craftable_goods = []

        for fg_code in batch_codes:
            def process_code():
                bom_data = fetch_bom_data(connection, fg_code)
                if not bom_data:
                    return None

                item_data, tree = build_bom_tree(bom_data, fg_code)
                quantity_to_check = craft_quantity or 1  # Use 1 if not specified
                return calculate_max_units(tree, item_data, fg_code, quantity_to_check)

            status, result = run_with_timeout(process_code, timeout=5)

            if status == 'timeout':
                print(f"Timeout processing {fg_code}")
                continue
            elif status == 'error':
                print(f"Error processing finished good {fg_code}: {result}")
                continue
            elif status == 'success' and result:
                max_units, shortages, used_items = result

                if craft_quantity:
                    if shortages:
                        for item in shortages:
                            cursor = connection.cursor()
                            cursor.execute("""
                                INSERT INTO non_craftable_list (bom_number, item_code, missing_qty, craft_attempt_qty, timestamp)
                                VALUES (%s, %s, %s, %s, CURRENT_DATE)
                            """, (fg_code, item[0], item[1], craft_quantity))
                            connection.commit()

                        non_craftable_goods.append({
                            "finished_good_code": fg_code,
                            "missing_items": [
                                {"item_code": item[0], "missing_qty": item[1]}
                                for item in shortages
                            ]
                        })
                    else:
                        craftable_goods.append({
                            "finished_good_code": fg_code,
                            "can_craft_quantity": craft_quantity
                        })
                else:
                    if shortages:
                        non_craftable_goods.append({
                            "finished_good_code": fg_code,
                            "missing_items": [item[0] for item in shortages]
                        })
                    elif max_units > 0:
                        craftable_goods.append({
                            "finished_good_code": fg_code,
                            "max_units": max_units
                        })

        return jsonify({
            "craftable_goods": craftable_goods,
            "non_craftable_goods": non_craftable_goods
        })

    except Exception as e:
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500

    finally:
        if connection:
            connection.close()

@app.route("/list_non_craftable", methods=["POST"])
@jwt_required()
def list_non_craftable_goods():
    try:
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT bom_number, item_code, missing_qty, craft_attempt_qty, TO_CHAR(timestamp, 'DD-MM-YYYY') as timestamp
            FROM non_craftable_list
            ORDER BY timestamp DESC
        """)
        data = cursor.fetchall()
        cursor.close()
        connection.close()

        return jsonify({"non_craftable_list": data})

    except Exception as e:
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500


@app.route("/download_non_craftable_list", methods=["POST"])
@jwt_required()
def download_non_craftable_list():
    try:
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT bom_number, item_code, missing_qty, craft_attempt_qty, TO_CHAR(timestamp, 'DD-MM-YYYY') as timestamp
            FROM non_craftable_list
            ORDER BY timestamp DESC
        """)
        data = cursor.fetchall()
        cursor.close()
        connection.close()

        if not data:
            return jsonify({"error": "No non-craftable data found"}), 404

        df = pd.DataFrame(data)

        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, sheet_name="Non Craftable List", index=False)
            worksheet = writer.sheets["Non Craftable List"]
            for i, col in enumerate(df.columns):
                col_width = max(df[col].astype(str).map(len).max(), len(col)) + 2
                worksheet.set_column(i, i, col_width)

        output.seek(0)
        base64_encoded = base64.b64encode(output.read()).decode("utf-8")

        return jsonify({"file_data": base64_encoded})

    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500

  



@app.route("/plan_crafted_good", methods=["POST"])
@jwt_required()
def plan_craftable_good():
    """
    Plans a craftable good by reducing inventory in `admin_parts`.
    Stores deductions in `planned_inventory`, and the planned BOM in `crafted_goods`.
    """
    try:
        data = request.get_json()
        bom_number = data.get("bom_number")
        quantity = data.get("quantity")

        if not bom_number or not isinstance(quantity, int) or quantity <= 0:
            return jsonify({"error": "Valid BOM number and quantity are required."}), 400

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 1. Fetch BOM and Build Tree
        bom_data = fetch_bom_data(connection, bom_number)
        if not bom_data:
            return jsonify({"error": "No BOM data found for the given BOM number"}), 404

        item_data, tree = build_bom_tree(bom_data, bom_number)

        # 2. Check Craftability
        _, shortages, used_items = calculate_max_units(tree, item_data, bom_number, quantity)
        if shortages:
            missing_items = [item[0] for item in shortages]
            return jsonify({"error": f"Not enough stock for items: {missing_items}"}), 400

        # 3. Deduct stock and update planned_inventory
        for item_code in used_items:
            required_qty = used_items[item_code]
            item_details = item_data[item_code]
            item_level = item_details["Item_Level"]
            extended_qty = item_details["Extended_Quantity"]

            # Fetch On_hand_Qty BEFORE deduction
            cursor.execute("""
                SELECT "On_hand_Qty" FROM admin_parts
                WHERE "Item_code" = %s AND bom_number = %s
            """, (item_code, bom_number))
            result = cursor.fetchone()
            admin_on_hand_qty = result["On_hand_Qty"] if result else 0

            # Prevent stock from becoming negative (just in case)
            if admin_on_hand_qty < required_qty:
                required_qty = admin_on_hand_qty

            # Deduct stock
            cursor.execute("""
                UPDATE admin_parts
                SET "On_hand_Qty" = GREATEST("On_hand_Qty" - %s, 0)
                WHERE "Item_code" = %s AND bom_number = %s
            """, (required_qty, item_code, bom_number))

            # Check if already in planned_inventory
            cursor.execute("""
                SELECT "Allocation" FROM planned_inventory
                WHERE "Item_code" = %s AND bom_number = %s
            """, (item_code, bom_number))
            existing = cursor.fetchone()

            net_qty = max(admin_on_hand_qty - required_qty, 0)

            if existing:
                new_alloc = existing["Allocation"] + required_qty
                cursor.execute("""
                    UPDATE planned_inventory
                    SET "Allocation" = %s,
                        "On_hand_Qty" = %s,
                        "Net_Qty" = %s
                    WHERE "Item_code" = %s AND bom_number = %s
                """, (new_alloc, admin_on_hand_qty, net_qty, item_code, bom_number))
            else:
                cursor.execute("""
                    INSERT INTO planned_inventory (
                        bom_number, "Item_Level", "Item_code", 
                        "Allocation", "Extended_Quantity", 
                        "On_hand_Qty", "Net_Qty"
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    bom_number, item_level, item_code,
                    required_qty, extended_qty,
                    admin_on_hand_qty, net_qty
                ))

        # 4. Log in crafted_goods
        cursor.execute("""
            INSERT INTO crafted_goods (bom_number, "On_hand_Qty")
            VALUES (%s, %s)
            ON CONFLICT (bom_number) DO NOTHING;
        """, (bom_number, quantity))

        connection.commit()
        cursor.close()

        return jsonify({
            "message": f"BOM {bom_number} planned successfully for {quantity} unit(s).",
            "planned_inventory_updated": True
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Database error: {str(e)}"}), 500



@app.route("/reset_approvals", methods=["POST"])
@jwt_required()
def reset_inventory():
    """
    Restores inventory from `planned_inventory` back to `admin_parts_duplicate`
    by summing up `Allocated` per `Item_code`. Also clears `planned_inventory`, `crafted_goods`, and `non_craftable_list`.
    """
    try:
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 🚀 **1. Restore Allocated Qty back to `admin_parts_duplicate`**
        cursor.execute("""
            UPDATE admin_parts
            SET "On_hand_Qty" = "On_hand_Qty" + sub.restock_qty
            FROM (
                SELECT "Item_code", SUM("Allocation") AS restock_qty
                FROM planned_inventory
                GROUP BY "Item_code"
            ) AS sub
            WHERE admin_parts."Item_code" = sub."Item_code";
        """)

        # 🚀 **2. Clear `planned_inventory`**
        cursor.execute("DELETE FROM planned_inventory")

        # 🚀 **3. Clear `crafted_goods`**
        cursor.execute("DELETE FROM crafted_goods")

        # 🚀 **4. Clear `non_craftable_list`**
        cursor.execute("DELETE FROM non_craftable_list")

        connection.commit()
        cursor.close()

        return jsonify({"message": "Planned inventory and non-craftable list reset successfully."}), 200

    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500


# @app.route("/reset_approvals", methods=["POST"])
# @jwt_required()
# def reset_inventory():
#     """
#     Restores inventory from `planned_inventory` back to `admin_parts`
#     and clears planned inventory + crafted goods.
#     """
#     try:
#         connection = connect_to_database()
#         if not connection:
#             return jsonify({"error": "Database connection failed"}), 500

#         cursor = connection.cursor()

#         # 🚀 **1. Restore On-hand Qty from `planned_inventory` to `admin_parts`**
#         cursor.execute("""
#             UPDATE admin_parts
#             SET "On_hand_Qty" = "On_hand_Qty" + sub.restock_qty
#             FROM (
#                 SELECT "Item_code", SUM("On_hand_Qty") AS restock_qty
#                 FROM planned_inventory
#                 GROUP BY "Item_code"
#             ) AS sub
#             WHERE admin_parts."Item_code" = sub."Item_code";
#         """)

#         # 🚀 **2. Clear `planned_inventory`**
#         cursor.execute("DELETE FROM planned_inventory")

#         # 🚀 **3. Clear `crafted_goods`**
#         cursor.execute("DELETE FROM crafted_goods")

#         connection.commit()
#         cursor.close()

#         return jsonify({"message": "Planned inventory reset successfully."}), 200

#     except Exception as e:
#         return jsonify({"error": f"Database error: {e}"}), 500




@app.route("/download_bom_data", methods=["POST"])
@jwt_required()
def download_bom_data():
    """
    Downloads the BOM data table with selected columns.
    Returns a Base64-encoded Excel file.
    """
    try:
        data = request.get_json()
        bom_number = data.get("bom_number", None)  # Optional

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)

        if bom_number:
            cursor.execute("""
                SELECT "Item_code", "On_hand_Qty", "Extended_Quantity"
                FROM admin_parts WHERE bom_number = %s
            """, (bom_number,))
        else:
            cursor.execute("""
                SELECT "Item_code", "On_hand_Qty", "Extended_Quantity"
                FROM admin_parts
            """)

        bom_data = cursor.fetchall()
        cursor.close()

        if not bom_data:
            return jsonify({"error": "No data found"}), 404

        # Convert to DataFrame
        df = pd.DataFrame(bom_data)

        # Create Excel file with adjusted column widths
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="BOM Data")

            # ✅ Auto-adjust column widths
            worksheet = writer.sheets["BOM Data"]
            for col_num, value in enumerate(df.columns.values):
                col_width = max(df[value].astype(str).map(len).max(), len(value)) + 2
                worksheet.set_column(col_num, col_num, col_width)

        output.seek(0)

        # Convert to Base64
        encoded_string = base64.b64encode(output.getvalue()).decode('utf-8')

        return jsonify({"file_data": encoded_string})

    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500
    
@app.route("/download_planned_inventory", methods=["POST"])
@jwt_required()
def download_planned_inventory():
    """
    Downloads the entire planned inventory table or for a specific BOM number.
    """
    try:
        data = request.get_json()
        bom_number = data.get("bom_number")

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)

        # 🚀 **1. Fetch planned inventory data**
        query = """
            SELECT id, bom_number, "Item_Level", "Item_code", "On_hand_Qty", 
                   "Extended_Quantity", "Allocation", "Net_Qty"
            FROM planned_inventory
        """
        params = ()
        if bom_number:
            query += " WHERE bom_number = %s"
            params = (bom_number,)

        cursor.execute(query, params)
        planned_data = cursor.fetchall()
        cursor.close()
        connection.close()

        if not planned_data:
            return jsonify({"error": "No planned inventory data found"}), 404

        # Convert to DataFrame
        df_planned = pd.DataFrame(planned_data)

        # 🚀 **2. Create an Excel File**
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_planned.to_excel(writer, sheet_name="Planned Inventory", index=False)

            # Adjust column widths
            worksheet = writer.sheets["Planned Inventory"]
            for i, col in enumerate(df_planned.columns):
                col_width = max(df_planned[col].astype(str).map(len).max(), len(col)) + 2
                worksheet.set_column(i, i, col_width)

        output.seek(0)

        # 🚀 **3. Convert to Base64**
        base64_encoded = base64.b64encode(output.read()).decode("utf-8")

        return jsonify({"file_data": base64_encoded})

    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500
        

        
@app.route("/assemble", methods=["POST"])
@jwt_required()
def assemble():
    """
    Confirms a planned BOM and deducts stock permanently.
    Removes it from `planned_inventory` and logs the assembly.
    """
    try:
        data = request.get_json()
        finished_good_code = data.get("finished_good_code")
        quantity = data.get("quantity")
        user = get_jwt_identity()

        if not finished_good_code or not isinstance(quantity, int) or quantity <= 0:
            return jsonify({"error": "Valid BOM number and quantity required."}), 400

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 🚀 **2. Remove from `planned_inventory`**
        cursor.execute("DELETE FROM planned_inventory WHERE bom_number = %s", (finished_good_code,))

        # 🚀 **3. Remove from `crafted_goods`**
        cursor.execute("DELETE FROM crafted_goods WHERE bom_number = %s", (finished_good_code,))

        # 🚀 **4. Log Assembly**
        cursor.execute("""
            INSERT INTO assembly_logs (bom_number, max_crafted_units, created_by, created_at)
            VALUES (%s, %s, %s, NOW());
        """, (finished_good_code, quantity, user))

        connection.commit()
        cursor.close()

        return jsonify({
            "message": f"Successfully assembled {finished_good_code} in {quantity} quantity.",
            "inventory_updated": True
        }), 200

    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500


# ✅ **List Assembly Logs with Search & Pagination**
@app.route("/assembly_logs", methods=["POST"])
@jwt_required()
def list_assembly_logs():
    try:
        data = request.get_json()
        search_text = data.get("search_text", "").strip()
        page = data.get("page", 1)  # Default page is 1
        page_size = data.get("page_size", 10)  # Default page size is 10
       
        # Validate pagination values
        if not isinstance(page, int) or page <= 0:
            return jsonify({"error": "Invalid page number"}), 400
        if not isinstance(page_size, int) or page_size <= 0:
            return jsonify({"error": "Invalid page size"}), 400
       
        offset = (page - 1) * page_size  # Calculate offset for pagination
       
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500
       
        cursor = connection.cursor()
       
        # Base query
        query = "SELECT id, bom_number, max_crafted_units, created_by, created_at FROM assembly_logs"
        params = []
       
        # Apply search filter if provided
        if search_text:
            query += " WHERE bom_number ILIKE %s OR created_by ILIKE %s"
            params.extend([f"%{search_text}%", f"%{search_text}%"])
        
        # Count total rows before applying pagination
        count_query = "SELECT COUNT(*) AS total FROM assembly_logs"
        if search_text:
            count_query += " WHERE bom_number ILIKE %s OR created_by ILIKE %s"
        
        cursor.execute(count_query, tuple(params[:2] if search_text else []))
        count_result = cursor.fetchone()
        
        # Handle different return types safely
        total_count = 0
        if count_result is not None:
            if isinstance(count_result, dict):
                total_count = count_result.get('total', 0)
            elif isinstance(count_result, (list, tuple)) and len(count_result) > 0:
                total_count = count_result[0]
            else:
                total_count = 0
       
        # Add pagination
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([page_size, offset])
       
        cursor.execute(query, tuple(params))
        logs = cursor.fetchall()
        cursor.close()
        connection.close()
       
        return jsonify({
            "assembly_logs": logs, 
            "page": page, 
            "page_size": page_size,
            "total_count": total_count
        }), 200
   
    except Exception as e:
        import traceback
        print(f"API error: {str(e)}")
        print(traceback.format_exc())
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500

@app.route("/admin_parts", methods=["POST"])
@jwt_required()
def get_admin_parts():
    data = request.get_json()
    query = data.get("searchtext", "").strip()
    page = data.get("page", 1)
    page_size = data.get("page_size", 10)

    if not isinstance(page, int) or not isinstance(page_size, int) or page <= 0 or page_size <= 0:
        return jsonify({"error": "Invalid page or page_size"}), 400

    offset = (page - 1) * page_size
    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        if query:
            search_pattern = f"%{query}%"

            cursor.execute("""
                SELECT COUNT(*) FROM admin_parts 
                WHERE CAST("bom_number" AS TEXT) ILIKE %s 
                OR "Item_code" ILIKE %s
                OR "description" ILIKE %s
                OR "Type" ILIKE %s
            """, (search_pattern, search_pattern, search_pattern, search_pattern))
            total_records = cursor.fetchone()['count']

            cursor.execute("""
                SELECT * FROM admin_parts 
                WHERE CAST("bom_number" AS TEXT) ILIKE %s 
                OR "Item_code" ILIKE %s
                OR "description" ILIKE %s
                OR "Type" ILIKE %s
                LIMIT %s OFFSET %s
            """, (search_pattern, search_pattern, search_pattern, search_pattern, page_size, offset))
        else:
            cursor.execute("SELECT COUNT(*) FROM admin_parts")
            total_records = cursor.fetchone()['count']

            cursor.execute("SELECT * FROM admin_parts LIMIT %s OFFSET %s", (page_size, offset))

        results = cursor.fetchall()

        return jsonify({
            "total_records": total_records,
            "page": page,
            "page_size": page_size,
            "results": results
        })
    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500
    finally:
        cursor.close()
        connection.close()






@app.route("/crafted_goods", methods=["POST"])
@jwt_required()
def fetch_or_search_crafted_goods():
    """
    Fetches or searches crafted goods with pagination.
    Filters only non-approved records.
    Orders results by newest first.
    """
    data = request.get_json()
    query = data.get("searchtext", "").strip()  
    page = data.get("page", 1)
    page_size = data.get("page_size", 10)

    if not isinstance(page, int) or not isinstance(page_size, int) or page <= 0 or page_size <= 0:
        return jsonify({"error": "Invalid page or page_size"}), 400

    offset = (page - 1) * page_size
    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        params = []
        query_conditions = "WHERE approved = FALSE"

        if query:
            search_pattern = f"%{query}%"
            if query.isnumeric():
                query_conditions += " AND bom_number = %s"
                params.append(query)
            else:
                query_conditions += " AND (CAST(bom_number AS TEXT) ILIKE %s OR description ILIKE %s OR \"Type\" ILIKE %s)"
                params.extend([search_pattern, search_pattern, search_pattern])

        # 🔹 Get total record count
        count_query = f"SELECT COUNT(*) FROM crafted_goods {query_conditions}"
        cursor.execute(count_query, tuple(params))
        total_records = cursor.fetchone()['count']

        # 🔹 Fetch paginated results sorted by newest first (Using Primary Key or Insert Timestamp)
        data_query = f"""
            SELECT * FROM crafted_goods 
            {query_conditions}
            ORDER BY id DESC  -- Ensure the latest records appear first
            LIMIT %s OFFSET %s
        """
        params.extend([page_size, offset])
        cursor.execute(data_query, tuple(params))
        results = cursor.fetchall()

        return jsonify({
            "total_records": total_records,
            "page": page,
            "page_size": page_size,
            "results": results
        })
    except Exception as e:
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500
    finally:
        cursor.close()
        connection.close()


@app.route("/approved_crafted_goods", methods=["POST"])
@jwt_required()
def fetch_approved_crafted_goods():
    data = request.get_json()
    query = data.get("searchtext", "").strip()
    page = data.get("page", 1)
    page_size = data.get("page_size", 10)

    if not isinstance(page, int) or not isinstance(page_size, int) or page <= 0 or page_size <= 0:
        return jsonify({"error": "Invalid page or page_size"}), 400

    offset = (page - 1) * page_size
    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        if query:
            search_pattern = f"%{query}%"

            cursor.execute("""
                SELECT COUNT(*) FROM crafted_goods 
                WHERE (bom_number ILIKE %s OR description ILIKE %s OR "Type" ILIKE %s) 
                AND approved = TRUE
            """, (search_pattern, search_pattern, search_pattern))

            total_records = cursor.fetchone()['count']

            cursor.execute("""
                SELECT * FROM crafted_goods 
                WHERE (bom_number ILIKE %s OR description ILIKE %s OR "Type" ILIKE %s) 
                AND approved = TRUE
                LIMIT %s OFFSET %s
            """, (search_pattern, search_pattern, search_pattern, page_size, offset))
        else:
            cursor.execute("SELECT COUNT(*) FROM crafted_goods WHERE approved = TRUE")
            total_records = cursor.fetchone()['count']

            cursor.execute("SELECT * FROM crafted_goods WHERE approved = TRUE LIMIT %s OFFSET %s", (page_size, offset))

        results = cursor.fetchall()

        return jsonify({
            "total_records": total_records,
            "page": page,
            "page_size": page_size,
            "results": results
        })
    except Exception as e:
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500
    finally:
        cursor.close()
        connection.close()

@app.route("/thumbsup", methods=["POST"])
@jwt_required()
def approve_crafted_good():
    data = request.get_json()
    bom_number = data.get("bom_number")

    if not bom_number:
        return jsonify({"error": "Missing bom_number"}), 400

    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        # Check if the item exists
        cursor.execute("SELECT * FROM crafted_goods WHERE bom_number = %s", (bom_number,))
        result = cursor.fetchone()

        if not result:
            return jsonify({"error": "Item not found"}), 404

        # Set approved = TRUE
        cursor.execute("UPDATE crafted_goods SET approved = TRUE WHERE bom_number = %s", (bom_number,))
        connection.commit()

        return jsonify({"message": "Item approved", "bom_number": bom_number, "approved": True})
    
    except Exception as e:
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500
    finally:
        cursor.close()
        connection.close()


# Add/Edit Admin Parts API
@app.route("/admin_parts/add_edit", methods=["POST"])
@jwt_required()
def add_edit_admin_parts():
    data = request.get_json()
    current_user = get_jwt_identity()
    
    # Check if ID exists for update operation
    record_id = data.get("id")

    # Fetch fields from request
    fields = {
        "bom_number": (data.get("bom_number"), int, "integer"),
        "item_code": (data.get("Item_code"), str, "text"),
        "item_level": (data.get("Item_Level", 0), int, "integer"),
        "description": (data.get("description", ""), str, "text"),
        "type": (data.get("Type"), str, "text"),
        "on_hand_qty": (data.get("On_hand_Qty", 0.0), float, "float"),
        "extended_quantity": (data.get("Extended_Quantity", 1.0), float, "float"),
        "is_active": (data.get("is_active", True), bool, "boolean"),
    }

    # Validate required fields
    missing_fields = [key for key in ["bom_number", "item_code", "type"] if not fields[key][0]]
    if missing_fields:
        return jsonify({
            "error": "Required fields missing",
            "missing_fields": missing_fields
        }), 400

    # Type validation
    errors = []
    validated_data = {}

    for field, (value, expected_type, expected_type_str) in fields.items():
        try:
            validated_data[field] = expected_type(value)
        except (ValueError, TypeError):
            errors.append(f"{field} data type incorrect, expected {expected_type_str}")

    if record_id:
        try:
            validated_data["id"] = int(record_id)
        except ValueError:
            errors.append("id data type incorrect, expected integer")

    if errors:
        return jsonify({
            "error": "Invalid data types provided",
            "details": errors
        }), 400

    # Database connection
    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        
        if "id" in validated_data:
            # UPDATE existing record
            cursor.execute("""
                UPDATE admin_parts SET
                    bom_number = %s,
                    "Item_code" = %s,
                    "Item_Level" = %s,
                    description = %s,
                    "Type" = %s,
                    "On_hand_Qty" = %s,
                    "Extended_Quantity" = %s,
                    is_active = %s
                WHERE id = %s
                RETURNING *;
            """, (
                validated_data["bom_number"],
                validated_data["item_code"],
                validated_data["item_level"],
                validated_data["description"],
                validated_data["type"],
                validated_data["on_hand_qty"],
                validated_data["extended_quantity"],
                validated_data["is_active"],
                validated_data["id"]
            ))

            if cursor.rowcount == 0:
                return jsonify({"error": f"No record found with id {validated_data['id']}"}), 404
                
        else:
            # INSERT new record
            cursor.execute("""
                INSERT INTO admin_parts (
                    bom_number,
                    "Item_code",
                    "Item_Level",
                    description,
                    "Type",
                    "On_hand_Qty",
                    "Extended_Quantity",
                    is_active,
                    created_date,
                    created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, %s)
                RETURNING *;
            """, (
                validated_data["bom_number"],
                validated_data["item_code"],
                validated_data["item_level"],
                validated_data["description"],
                validated_data["type"],
                validated_data["on_hand_qty"],
                validated_data["extended_quantity"],
                validated_data["is_active"],
                current_user
            ))

        result = cursor.fetchone()
        connection.commit()
        
        operation = "updated" if "id" in validated_data else "created"
        return jsonify({
            "message": f"Admin part {operation} successfully",
            "data": result
        }), 200
        
    except psycopg2.Error as e:
        connection.rollback()
        return jsonify({
            "error": "Database error",
            "details": str(e)
        }), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

        
if __name__ == "__main__":
    app.run(debug=True, host = '0.0.0.0', port = 5001)
