![9e057a7e-dffe-4ee4-b233-bd28ca3fde0c](https://github.com/user-attachments/assets/183ae9ee-6922-40f9-b8e2-b60c414b48b6)

# 🧠 CnS Electric API - BOM & Stock Management System

An API-powered backend system that processes Bill of Materials (BOM) and current stock levels to determine how many finished goods can be assembled. This terminal-based system supports Excel BOM input, real-time stock comparison, and updates inventory post-production. Designed for manufacturers and engineers managing multi-level production workflows.

---

## 🔧 Tech Stack

- **Backend**: Python (Flask)
- **Database**: Supabase (for POC)
- **ORM**: pyodbc 

---

## ✨ Core Features

- 📥 Accept dynamic Excel BOM files
- 🔎 Analyze part usage vs stock availability
- 🧮 Compute maximum producible units
- ⚠️ Highlight low or missing inventory parts
- ✅ Confirm and update inventory after production
- 📊 Unified SQL table for BOM and inventory
- 🧹 Cleans BOM files and extracts only required columns
- 🖥️ Command-line friendly with full API access

---

## 🧠 Inventory Logic

To calculate the max possible production quantity:

```python
max_units = min(on_hand_qty[i] // extended_qty[i] for i in required_parts)
```

---

## 🧩 How It Works

The Inventory Planner system builds an in-memory tree structure from the BOM file to represent multi-level dependencies between finished goods and their components. It then performs a bottom-up traversal to calculate the total required quantities for each part and determine how many units can be assembled from the available stock.

### 🔗 Step-by-Step Breakdown

📄 **BOM File Parsing**

The uploaded Excel BOM is cleaned to extract:

Code: Parent (finished good or sub-assembly)
Level: BOM depth (0 = root product)
Item Code: Child/Component
Extended Quantity: How many units of the item are needed

🌳 **Tree Construction**

A graph/tree-like structure is created where each node represents an item.
Each item maintains links to its children (components).

For example:
```
FG01
├── P01
└── P02
├── P02A
└── P02B
```

🔁 **Traversal & Quantity Propagation**

A recursive traversal is done from the root (finished good) to the leaves.
At each level, the required quantity is multiplied based on the parent’s requirement, resulting in total required quantity per item.
This helps flatten the BOM tree into a usable structure for comparison with inventory.

📊 **Inventory Matching**

For each leaf node (raw part), the planner checks the On-hand Qty from the database.
It then calculates how many full units can be assembled using:
```
max_units = min(on_hand_qty[i] // total_required_qty[i])
```

⚠️ **Bottleneck Detection**

If any part has insufficient stock, it’s flagged in the missing_items list.
The system shows how many units can be built and what's stopping further production.

✅ **Inventory Update**

Upon confirmation from the user/API, the system:
Reduces the on-hand quantity for each part based on the actual production.

---

## 🛠️ Setup Instructions

**1. Clone the Repository**
```
git clone https://github.com/yourusername/inventory-planner-api.git
cd inventory-planner-api
```
**2. Install Dependencies**
```
pip install -r requirements.txt
```
**3. Configure the Database**
Edit your connection inside db.py or use environment variables:
```
DB_SERVER=your_sql_server
DB_NAME=your_database
DB_USER=your_username
DB_PASSWORD=your_password
```
**4. Run the API**
```python
python app.py
```


