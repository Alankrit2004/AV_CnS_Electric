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
        
        # ✅ Fetch all finished goods without unnecessary limits
        cursor.execute('''
            SELECT DISTINCT "bom_number" FROM "admin_parts"
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
                
                # ✅ Build BOM tree correctly
                item_data, tree = build_bom_tree(bom_data, fg_code)

                # 🔍 Debugging - Check tree structure
                print(f"\n🔍 Processing {fg_code}")
                print(f"📌 BOM Tree: {tree}")
                print(f"📌 Item Data: {item_data}")

                max_units, shortages = calculate_max_units(tree, item_data, fg_code, 1)
                
                return (max_units, shortages)

            # ✅ Increase timeout to avoid missing deep BOM structures
            status, result = run_with_timeout(process_code, timeout=10)

            if status == 'timeout':
                print(f"⏳ Timeout processing {fg_code} - Increase timeout if needed")
                continue
            elif status == 'error':
                print(f"❌ Error processing {fg_code}: {result}")
                continue
            elif status == 'success' and result:
                max_units, shortages = result
                
                if shortages:
                    missing_items = [item[0] for item in shortages]
                    non_craftable_goods.append({"finished_good_code": fg_code, "missing_items": missing_items})
                elif max_units > 0:
                    craftable_goods.append({"finished_good_code": fg_code, "max_units": max_units})

        # ✅ Store in database
        store_craftable_non_craftable_goods(connection, craftable_goods, non_craftable_goods)

        return jsonify({
            "craftable_goods": craftable_goods,
            "non_craftable_goods": non_craftable_goods
        })
   
    except Exception as e:
        print(f"❌ Error in get_craftable_goods: {str(e)}")
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


# ✅ **Assemble Finished Goods API**
@app.route("/assemble", methods=["POST"])
@jwt_required()
def assemble():
    try:
        data = request.get_json()
        print(f"Received data: {data}")  # Debug line
        
        finished_good_code = data.get("finished_good_code")
        quantity = data.get("quantity")
        confirm = data.get("confirm", False)
        
        print(f"Parsed values - code: {finished_good_code}, quantity: {quantity}, confirm: {confirm}")  # Debug line
        
        if not finished_good_code or not isinstance(quantity, int) or quantity <= 0:
            return jsonify({"error": "Invalid input data"}), 400
            
        connection = connect_to_database()
        if not connection:
            return jsonify({"error": "Database connection failed"}), 500
            
        result = assemble_finished_good(
            connection=connection,
            finished_good_code=finished_good_code,
            quantity=quantity,
            confirm=confirm
        )
        
        print(f"Function result: {result}")  # Debug line
        
        connection.close()
        
        if result["success"]:
            return jsonify({"message": result["message"]}), 200
        else:
            return jsonify({"error": result["message"], "details": result}), 400
            
    except Exception as e:
        print(f"API error: {str(e)}")  # Debug line
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
            if query.isnumeric():
                cursor.execute("""
                    SELECT COUNT(*) FROM admin_parts 
                    WHERE "bom_number" = %s::bigint
                """, (query,))
                total_records = cursor.fetchone()['count']

                cursor.execute("""
                    SELECT * FROM admin_parts 
                    WHERE "bom_number" = %s::bigint
                    LIMIT %s OFFSET %s
                """, (query, page_size, offset))
            else:
                cursor.execute("""
                    SELECT COUNT(*) FROM admin_parts 
                    WHERE CAST("bom_number" AS TEXT) ILIKE %s 
                    OR "description" ILIKE %s
                    OR "Type" ILIKE %s
                """, (f"%{query}%", f"%{query}%", f"%{query}%"))
                total_records = cursor.fetchone()['count']

                cursor.execute("""
                    SELECT * FROM admin_parts 
                    WHERE CAST("bom_number" AS TEXT) ILIKE %s 
                    OR "description" ILIKE %s
                    OR "Type" ILIKE %s
                    LIMIT %s OFFSET %s
                """, (f"%{query}%", f"%{query}%", f"%{query}%", page_size, offset))
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


@app.route("/non_craftable_goods", methods=["POST"])
@jwt_required()
def fetch_or_search_non_craftable_goods():
    data = request.get_json()
    query = data.get("searchtext", "").strip()  # Allow search filtering
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
            # If a search query is provided, filter results
            search_pattern = f"%{query}%"

            cursor.execute("""
                SELECT COUNT(*) FROM non_craftable_goods 
                WHERE CAST(bom_number AS TEXT) ILIKE %s 
                OR description ILIKE %s
                OR "Type" ILIKE %s
            """, (search_pattern, search_pattern, search_pattern))
            
            total_records = cursor.fetchone()['count']
            
            cursor.execute("""
                SELECT * FROM non_craftable_goods 
                WHERE CAST(bom_number AS TEXT) ILIKE %s 
                OR description ILIKE %s
                OR "Type" ILIKE %s
                LIMIT %s OFFSET %s
            """, (search_pattern, search_pattern, search_pattern, page_size, offset))

        else:
            # If no search query, fetch all records paginated
            cursor.execute("SELECT COUNT(*) FROM non_craftable_goods")
            total_records = cursor.fetchone()['count']
            
            cursor.execute("SELECT * FROM non_craftable_goods LIMIT %s OFFSET %s", (page_size, offset))
        
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



@app.route("/crafted_goods", methods=["POST"])
@jwt_required()
def fetch_or_search_crafted_goods():
    data = request.get_json()
    query = data.get("searchtext", "").strip()  # Allow search filtering
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
            # If a search query is provided, filter results
            search_pattern = f"%{query}%"
            
            if query.isnumeric():
                cursor.execute("""
                    SELECT COUNT(*) FROM crafted_goods 
                    WHERE bom_number = %s AND approved = FALSE
                """, (query,))
                
                total_records = cursor.fetchone()['count']
                
                cursor.execute("""
                    SELECT * FROM crafted_goods 
                    WHERE bom_number = %s AND approved = FALSE
                    LIMIT %s OFFSET %s
                """, (query, page_size, offset))
            else:
                cursor.execute("""
                    SELECT COUNT(*) FROM crafted_goods 
                    WHERE (bom_number ILIKE %s OR description ILIKE %s OR "Type" ILIKE %s) 
                    AND approved = FALSE
                """, (search_pattern, search_pattern, search_pattern))
                
                total_records = cursor.fetchone()['count']
                
                cursor.execute("""
                    SELECT * FROM crafted_goods 
                    WHERE (bom_number ILIKE %s OR description ILIKE %s OR "Type"" ILIKE %s) 
                    AND approved = FALSE
                    LIMIT %s OFFSET %s
                """, (search_pattern, search_pattern, search_pattern, page_size, offset))
        else:
            # If no search query, fetch only non-approved records paginated
            cursor.execute("SELECT COUNT(*) FROM crafted_goods WHERE approved = FALSE")
            total_records = cursor.fetchone()['count']
            
            cursor.execute("SELECT * FROM crafted_goods WHERE approved = FALSE LIMIT %s OFFSET %s", (page_size, offset))
        
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

# Add/Edit Non-Craftable FG API
@app.route("/non_craftable_goods/add_edit", methods=["POST"])
@jwt_required()
def add_edit_non_craftable_fg():
    data = request.get_json()

    # Extract fields
    item_id = data.get("id")  # Optional
    bom_number = data.get("bom_number")
    description = data.get("description")
    type = data.get("type")  
    on_hand_qty = data.get("on_hand_qty", 0)  
    is_active = data.get("is_active", False)

    # Validate required fields
    if not bom_number:
        return jsonify({"error": "Required fields missing", "missing_fields": ["bom_number"]}), 400

    connection = connect_to_database()
    if not connection:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cursor = connection.cursor()

        if item_id:
            # If id is provided, update the existing row
            cursor.execute("""
                UPDATE non_craftable_goods
                SET bom_number = %s, description = %s, "Type" = %s, "On_hand_Qty" = %s, is_active = %s
                WHERE id = %s
                RETURNING *;
            """, (bom_number, description, type, on_hand_qty, is_active, item_id))
        else:
            # If id is not provided, insert a new row
            cursor.execute("""
                INSERT INTO non_craftable_goods (bom_number, description, "Type", "On_hand_Qty", is_active)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *;
            """, (bom_number, description, type, on_hand_qty, is_active))

        result = cursor.fetchone()
        connection.commit()

        return jsonify({
            "message": "Non-craftable good updated successfully" if item_id else "Non-craftable good added successfully",
            "data": result
        }), 200

    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        cursor.close()
        connection.close()


        
if __name__ == "__main__":
    app.run(debug=True, host = '0.0.0.0', port = 5001)
