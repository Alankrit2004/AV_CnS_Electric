from flask import Flask, request, jsonify
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

# ✅ **Get Craftable & Non-Craftable Goods API**
@app.route("/get_craftable_goods", methods=["POST"])
@jwt_required()
def get_craftable_goods():
    connection = None
    try:
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT DISTINCT "bom_number" 
            FROM "admin_parts_duplicate" 
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
                    print(f"No BOM data found for {fg_code}")
                    return None
                
                item_data, tree = build_bom_tree(bom_data, fg_code)
                max_units, shortages = calculate_max_units(tree, item_data, fg_code, 1)
                
                return (max_units, shortages)
            
            status, result = run_with_timeout(process_code, timeout=5)
            
            if status == 'timeout':
                print(f"Timeout processing {fg_code}")
                continue
            elif status == 'error':
                print(f"Error processing finished good {fg_code}: {result}")
                continue
            elif status == 'success' and result:
                max_units, shortages = result
                if shortages:
                    missing_items = [item[0] for item in shortages]
                    non_craftable_goods.append((fg_code, missing_items))
                elif max_units > 0:
                    craftable_goods.append((fg_code, max_units))
        
        # Store the results in the database
        print(f"Craftable goods: {craftable_goods}")
        print(f"Non-craftable goods: {non_craftable_goods}")
        # store_craftable_non_craftable_goods(connection, craftable_goods, non_craftable_goods)
        
        response_craftable = [
            {"finished_good_code": fg_code, "max_units": max_units}
            for fg_code, max_units in craftable_goods
        ]
        
        response_non_craftable = [
            {"finished_good_code": fg_code, "missing_items": missing_items}
            for fg_code, missing_items in non_craftable_goods
        ]
        
        return jsonify({
            "craftable_goods": response_craftable,
            "non_craftable_goods": response_non_craftable
        })
   
    except Exception as e:
        print(f"Error in get_craftable_goods: {str(e)}")
        return jsonify({"error": f"Internal Server Error: {str(e)}"}), 500
   
    finally:
        if connection:
            connection.close()


# Timeout decorator
from contextlib import contextmanager
import signal

class TimeoutException(Exception):
    pass

@contextmanager
def timeout(seconds):
    def _handle_timeout(signum, frame):
        raise TimeoutError(f"Function call timed out after {seconds} seconds")
    
    # Set the signal handler and a alarm
    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    
    try:
        yield
    finally:
        # Cancel the alarm
        signal.alarm(0)


@app.route("/assemble", methods=["POST"])
@jwt_required()
def assemble():
    """
    Confirms a planned BOM and deducts stock permanently.
    Removes it from `planned_inventory` and logs the assembly.
    """
    try:
        data = request.get_json()
        bom_number = data.get("bom_number")
        quantity = data.get("quantity")
        user = get_jwt_identity()

        if not bom_number or not isinstance(quantity, int) or quantity <= 0:
            return jsonify({"error": "Valid BOM number and quantity required."}), 400

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 🚀 **1. Permanently Deduct Inventory**
        cursor.execute("""
            UPDATE admin_parts
            SET "On_hand_Qty" = "On_hand_Qty" - sub.deduct_qty
            FROM (
                SELECT "Item_code", SUM("On_hand_Qty") AS deduct_qty
                FROM planned_inventory
                WHERE bom_number = %s
                GROUP BY "Item_code"
            ) AS sub
            WHERE admin_parts."Item_code" = sub."Item_code";
        """, (bom_number,))

        # 🚀 **2. Remove from `planned_inventory`**
        cursor.execute("DELETE FROM planned_inventory WHERE bom_number = %s", (bom_number,))

        # 🚀 **3. Remove from `crafted_goods`**
        cursor.execute("DELETE FROM crafted_goods WHERE bom_number = %s", (bom_number,))

        # 🚀 **4. Log Assembly**
        cursor.execute("""
            INSERT INTO assembly_logs (bom_number, max_crafted_units, created_by, created_at)
            VALUES (%s, %s, %s, NOW());
        """, (bom_number, quantity, user))

        connection.commit()
        cursor.close()

        return jsonify({
            "message": f"Successfully assembled {bom_number} in {quantity} quantity.",
            "inventory_updated": True
        }), 200

    except Exception as e:
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500




# @app.route("/assemble", methods=["POST"])
# @jwt_required()
# def assemble():
#     try:
#         data = request.get_json()
#         print(f"🔹 Received Data: {data}")  # Debugging
        
#         bom_number = data.get("bom_number")
#         quantity = data.get("quantity")
#         user = get_jwt_identity()  # Logged-in user

#         print(f"🔍 Checking BOM: {bom_number}, Quantity: {quantity}, User: {user}")  # Debugging

#         if not bom_number or not isinstance(quantity, int) or quantity <= 0:
#             return jsonify({"error": "Valid BOM number and quantity are required."}), 400
            
#         connection = connect_to_database()
#         if not connection:
#             return jsonify({"error": "Database connection failed"}), 500

#         cursor = connection.cursor()

#         # 🚀 **1. Check if BOM exists in `crafted_goods`**
#         cursor.execute("SELECT max_units FROM crafted_goods WHERE bom_number = %s", (bom_number,))
#         crafted_good = cursor.fetchone()

#         if not crafted_good:
#             return jsonify({"error": f"BOM number {bom_number} not found in crafted_goods."}), 400

#         max_units = crafted_good[0]
#         if quantity > max_units:
#             return jsonify({"error": f"Cannot assemble {quantity}, max available: {max_units}"}), 400

#         # 🚀 **2. Remove from `crafted_goods` & `planned_inventory`**
#         cursor.execute("DELETE FROM crafted_goods WHERE bom_number = %s", (bom_number,))
#         cursor.execute("DELETE FROM planned_inventory WHERE bom_number = %s", (bom_number,))

#         # 🚀 **3. Log assembly in `assembly_logs`**
#         cursor.execute("""
#             INSERT INTO assembly_logs (bom_number, max_crafted_units, created_by, created_at)
#             VALUES (%s, %s, %s, NOW());
#         """, (bom_number, quantity, user))

#         connection.commit()
#         cursor.close()

#         return jsonify({
#             "message": f"Successfully assembled {bom_number} in {quantity} quantity.",
#             "inventory_updated": True
#         }), 200

#     except Exception as e:
#         print(f"❌ API Error: {str(e)}")  # Debugging
#         return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500





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
        
        # Add pagination
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([page_size, offset])
        
        cursor.execute(query, tuple(params))
        logs = cursor.fetchall()
        cursor.close()
        connection.close()
        
        return jsonify({"assembly_logs": logs, "page": page, "page_size": page_size}), 200
    
    except Exception as e:
        print(f"API error: {str(e)}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@app.route("/plan_crafted_good", methods=["POST"])
@jwt_required()
def plan_craftable_good():
    """
    Plans a craftable good by reducing inventory in `admin_parts` for ALL occurrences of an `Item_code`.
    The reductions are stored in `planned_inventory` and the planned BOM is stored in `crafted_goods`.
    """
    try:
        data = request.get_json()
        bom_number = data.get("bom_number")
        max_units = data.get("max_units")

        if not bom_number or not isinstance(max_units, int) or max_units <= 0:
            return jsonify({"error": "Valid BOM number and max_units are required."}), 400

        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 🚀 **1. Fetch BOM Data and Build Tree**
        bom_data = fetch_bom_data(connection, bom_number)
        if not bom_data:
            return jsonify({"error": "No BOM data found for the given BOM number"}), 404

        item_data, tree = build_bom_tree(bom_data, bom_number)

        # 🚀 **2. Check Stock Availability Before Reduction**
        _, shortages = calculate_max_units(tree, item_data, bom_number, max_units)

        if shortages:
            missing_items = [item[0] for item in shortages]
            return jsonify({"error": f"Not enough stock for items: {missing_items}"}), 400

        # 🚀 **3. Reduce Stock in `admin_parts` and Track in `planned_inventory`**
        for item_code, item_details in item_data.items():
            required_qty = item_details["Extended_Quantity"] * max_units
            available_qty = item_details["On_hand_Qty"]

            if available_qty >= required_qty:
                # 🔹 Reduce stock in `admin_parts` for **ALL BOMs** using this `Item_code`
                cursor.execute("""
                    UPDATE admin_parts
                    SET "On_hand_Qty" = "On_hand_Qty" - %s
                    WHERE "Item_code" = %s
                """, (required_qty, item_code))

                # 🔹 Check if item exists in `planned_inventory`
                cursor.execute("""
                    SELECT "On_hand_Qty" FROM planned_inventory
                    WHERE "Item_code" = %s
                """, (item_code,))
                existing_record = cursor.fetchone()

                if existing_record:
                    # 🔹 Update planned_inventory (SUM the reductions)
                    cursor.execute("""
                        UPDATE planned_inventory
                        SET "On_hand_Qty" = "On_hand_Qty" + %s
                        WHERE "Item_code" = %s
                    """, (required_qty, item_code))
                else:
                    # 🔹 Insert new record in `planned_inventory`
                    cursor.execute("""
                        INSERT INTO planned_inventory (bom_number, "Item_code", "On_hand_Qty", "Extended_Quantity")
                        VALUES (%s, %s, %s, %s)
                    """, (bom_number, item_code, required_qty, item_details["Extended_Quantity"]))

        # 🚀 **4. Store Planned BOM in `crafted_goods`**
        cursor.execute("""
            INSERT INTO crafted_goods (bom_number, "On_hand_Qty")
            VALUES (%s, %s)
            ON CONFLICT (bom_number) DO UPDATE 
            SET "On_hand_Qty" = EXCLUDED."On_hand_Qty";
        """, (bom_number, max_units))

        connection.commit()
        cursor.close()

        return jsonify({
            "message": f"BOM number {bom_number} planned successfully with {max_units} max units.",
            "planned_inventory_updated": True
        }), 200

    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500




# @app.route("/plan_crafted_good", methods=["POST"])
# @jwt_required()
# def plan_craftable_good():
#     """
#     Stores the selected craftable good in the crafted_goods table,
#     while reducing stock from admin_parts for ALL occurrences of the same Item_code.
#     """
#     data = request.get_json()
#     bom_number = data.get("bom_number")
#     max_units = data.get("max_units")

#     if not bom_number or not isinstance(max_units, int) or max_units <= 0:
#         return jsonify({"error": "Valid BOM number and max_units are required."}), 400

#     try:
#         connection = connect_to_database()
#         if not connection:
#             return jsonify({"error": "Database connection failed"}), 500

#         cursor = connection.cursor()

#         # 🚀 **1. Fetch BOM data and build the tree**
#         bom_data = fetch_bom_data(connection, bom_number)
#         if not bom_data:
#             return jsonify({"error": "No BOM data found for the given BOM number"}), 404

#         item_data, tree = build_bom_tree(bom_data, bom_number)

#         # 🚀 **2. Check stock availability recursively**
#         _, shortages = calculate_max_units(tree, item_data, bom_number, max_units)

#         if shortages:
#             missing_items = [item[0] for item in shortages]
#             return jsonify({"error": f"Not enough stock for items: {missing_items}"}), 400

#         # 🚀 **3. Reduce stock in `admin_parts` and track reductions in `planned_inventory`**
#         for item_code, item_details in item_data.items():
#             required_qty = item_details["Extended_Quantity"] * max_units
#             available_qty = item_details["On_hand_Qty"]

#             if available_qty >= required_qty:
#                 # 🔹 Update `admin_parts` by reducing stock for **ALL BOMs** using this `Item_code`
#                 cursor.execute("""
#                     UPDATE admin_parts
#                     SET "On_hand_Qty" = "On_hand_Qty" - %s
#                     WHERE "Item_code" = %s
#                 """, (required_qty, item_code))

#                 # 🔹 Check if entry exists in `planned_inventory`
#                 cursor.execute("""
#                     SELECT "On_hand_Qty" FROM planned_inventory
#                     WHERE bom_number = %s AND "Item_code" = %s
#                 """, (bom_number, item_code))
#                 existing_record = cursor.fetchone()

#                 if existing_record:
#                     # 🔹 Update planned_inventory (SUM the reductions)
#                     cursor.execute("""
#                         UPDATE planned_inventory
#                         SET "On_hand_Qty" = "On_hand_Qty" + %s
#                         WHERE bom_number = %s AND "Item_code" = %s
#                     """, (required_qty, bom_number, item_code))
#                 else:
#                     # 🔹 Insert new record in `planned_inventory`
#                     cursor.execute("""
#                         INSERT INTO planned_inventory (bom_number, "Item_code", "On_hand_Qty", "Extended_Quantity")
#                         VALUES (%s, %s, %s, %s)
#                     """, (bom_number, item_code, required_qty, item_details["Extended_Quantity"]))

#         # 🚀 **4. Store planned BOM in `crafted_goods`**
#         cursor.execute("""
#             INSERT INTO crafted_goods (bom_number, "On_hand_Qty")
#             VALUES (%s, %s)
#             ON CONFLICT (bom_number) DO UPDATE 
#             SET "On_hand_Qty" = EXCLUDED."On_hand_Qty";
#         """, (bom_number, max_units))

#         connection.commit()
#         cursor.close()

#         return jsonify({
#             "message": f"BOM number {bom_number} planned successfully with {max_units} max units.",
#             "planned_inventory_updated": True
#         })

#     except Exception as e:
#         return jsonify({"error": f"Database error: {e}"}), 500





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
    
    # Get required fields with proper case matching
    bom_number = data.get("bom_number")  # int8
    item_code = data.get("Item_code")    # varchar
    item_level = data.get("Item_Level", 0)  # int4
    description = data.get("description", "") # text
    type = data.get("Type")              # varchar
    on_hand_qty = data.get("On_hand_Qty", 0.0)  # float8
    extended_quantity = data.get("Extended_Quantity", 1.0)  # float8
    is_active = data.get("is_active", True)  # bool

    # Validate required fields and types
    if not all([bom_number, item_code, type]):
        return jsonify({
            "error": "Required fields missing",
            "required": ["bom_number", "Item_code", "Type"]
        }), 400

    try:
        # Type validation
        bom_number = int(bom_number)
        item_level = int(item_level)
        on_hand_qty = float(on_hand_qty)
        extended_quantity = float(extended_quantity)
        is_active = bool(is_active)
        if record_id:
            record_id = int(record_id)

    except (ValueError, TypeError):
        return jsonify({"error": "Invalid data types provided"}), 400

    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor(cursor_factory=RealDictCursor)
        
        if record_id:
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
                bom_number,
                item_code,
                item_level,
                description,
                type,
                on_hand_qty,
                extended_quantity,
                is_active,
                record_id
            ))
            
            if cursor.rowcount == 0:
                return jsonify({"error": f"No record found with id {record_id}"}), 404
                
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
                bom_number,
                item_code,
                item_level,
                description,
                type,
                on_hand_qty,
                extended_quantity,
                is_active,
                current_user
            ))

        result = cursor.fetchone()
        connection.commit()
        
        operation = "updated" if record_id else "created"
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


# @app.route("/reset_approvals", methods=["POST"])
# @jwt_required()
# def reset_inventory():
#     """
#     Restores inventory from `planned_inventory` back to `admin_parts`,
#     then clears `planned_inventory` and `crafted_goods`.
#     """
#     try:
#         connection = connect_to_database()
#         if not connection:
#             return jsonify({"error": "Database connection failed"}), 500

#         cursor = connection.cursor()

#         # 🚀 **1. Add back planned reductions to admin_parts**
#         cursor.execute("""
#             UPDATE admin_parts AS ap
#             SET "On_hand_Qty" = ap."On_hand_Qty" + pi."On_hand_Qty"
#             FROM planned_inventory AS pi
#             WHERE ap."Item_code" = pi."Item_code";
#         """)

#         # 🚀 **2. Clear `planned_inventory` & `crafted_goods`**
#         cursor.execute("DELETE FROM planned_inventory")
#         cursor.execute("DELETE FROM crafted_goods")

#         connection.commit()
#         cursor.close()

#         return jsonify({
#             "message": "Inventory reset successfully. All planned reductions restored.",
#             "reset_done": True
#         }), 200

#     except Exception as e:
#         return jsonify({"error": f"Database error: {e}"}), 500


@app.route("/reset_approvals", methods=["POST"])
@jwt_required()
def reset_inventory():
    """
    Restores inventory from `planned_inventory` back to `admin_parts`
    and clears planned inventory + crafted goods.
    """
    try:
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500

        cursor = connection.cursor()

        # 🚀 **1. Restore On-hand Qty from `planned_inventory` to `admin_parts`**
        cursor.execute("""
            UPDATE admin_parts
            SET "On_hand_Qty" = "On_hand_Qty" + sub.restock_qty
            FROM (
                SELECT "Item_code", SUM("On_hand_Qty") AS restock_qty
                FROM planned_inventory
                GROUP BY "Item_code"
            ) AS sub
            WHERE admin_parts."Item_code" = sub."Item_code";
        """)

        # 🚀 **2. Clear `planned_inventory`**
        cursor.execute("DELETE FROM planned_inventory")

        # 🚀 **3. Clear `crafted_goods`**
        cursor.execute("DELETE FROM crafted_goods")

        connection.commit()
        cursor.close()

        return jsonify({"message": "Planned inventory reset successfully."}), 200

    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500



        
if __name__ == "__main__":
    app.run(debug=True, host = '0.0.0.0', port = 5001)
