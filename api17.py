import os
import ast
import re
import textwrap
from pathlib import Path
import sqlite3
import threading
import time
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import requests
import numpy as np
from sklearn.linear_model import LinearRegression

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

try:
    from google import genai
except ImportError:
    genai = None

DATABASE_PATH = "penzugyek.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY and genai:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        ai_client = None
else:
    ai_client = None

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def get_connection():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        check_same_thread=False
    )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection

def initialize_database():
    connection = get_connection()
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS penz(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipus TEXT NOT NULL,
                osszeg REAL NOT NULL,
                leiras TEXT,
                kat TEXT,
                datum TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS celok(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nev TEXT NOT NULL,
                celo REAL NOT NULL,
                felretett REAL DEFAULT 0,
                letrehozva TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS beallitasok(
                kulcs TEXT PRIMARY KEY,
                ertek TEXT
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_penz_datum ON penz(datum)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_penz_tipus ON penz(tipus)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_penz_kat ON penz(kat)")

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(celok)").fetchall()
        }
        if "letrehozva" not in columns:
            connection.execute("ALTER TABLE celok ADD COLUMN letrehozva TEXT")
        connection.commit()
    finally:
        connection.close()

def get_transactions():
    connection = get_connection()
    try:
        return connection.execute("""
            SELECT id, tipus, osszeg, leiras, kat, datum
            FROM penz
            ORDER BY id DESC
        """).fetchall()
    finally:
        connection.close()

def get_goals():
    connection = get_connection()
    try:
        return connection.execute("""
            SELECT id, nev, celo, felretett, letrehozva
            FROM celok
            ORDER BY id DESC
        """).fetchall()
    finally:
        connection.close()

def get_balance():
    connection = get_connection()
    try:
        result = connection.execute("""
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN tipus='Bevétel'
                            THEN osszeg
                            ELSE -osszeg
                        END
                    ), 0
                )
            FROM penz
        """).fetchone()
        return result[0] or 0
    finally:
        connection.close()

def get_month_statistics():
    now = datetime.now()
    month = now.strftime("%Y-%m")
    connection = get_connection()
    try:
        income = connection.execute("""
            SELECT COALESCE(SUM(osszeg),0)
            FROM penz
            WHERE tipus='Bevétel'
            AND datum LIKE ?
        """, (month + "%",)).fetchone()[0]

        expense = connection.execute("""
            SELECT COALESCE(SUM(osszeg),0)
            FROM penz
            WHERE tipus='Kiadás'
            AND datum LIKE ?
        """, (month + "%",)).fetchone()[0]

        return income, expense
    finally:
        connection.close()

def get_category_statistics():
    connection = get_connection()
    try:
        rows = connection.execute("""
            SELECT kat, SUM(osszeg)
            FROM penz
            WHERE tipus='Kiadás'
            GROUP BY kat
            ORDER BY SUM(osszeg) DESC
        """).fetchall()
        return rows
    finally:
        connection.close()

def get_machine_learning_predictions():
    connection = get_connection()
    try:
        rows = connection.execute("""
            SELECT SUBSTR(datum, 1, 7) as honap, SUM(osszeg)
            FROM penz
            WHERE tipus='Kiadás'
            GROUP BY honap
            ORDER BY honap ASC
        """).fetchall()
    finally:
        connection.close()

    if not rows:
        return [], [], []

    months = [row[0] for row in rows]
    actual_expenses = [row[1] for row in rows]

    if len(rows) < 2:
        return months, [actual_expenses[0] * 0.95], actual_expenses

    X = np.array(range(len(months))).reshape(-1, 1)
    y = np.array(actual_expenses)

    model = LinearRegression()
    model.fit(X, y)

    predicted_expenses = model.predict(X).tolist()
    predicted_expenses = [max(0.0, p) for p in predicted_expenses]

    return months, predicted_expenses, actual_expenses

def money(value):
    return f"{value:,.0f} Ft".replace(",", " ")

def calculate_score():
    income, expense = get_month_statistics()
    if income <= 0:
        return 50
    savings = income - expense
    saving_rate = savings / income
    score = 50

    if saving_rate >= 0.30:
        score += 30
    elif saving_rate >= 0.20:
        score += 20
    elif saving_rate >= 0.10:
        score += 10
    elif saving_rate < 0:
        score -= 25

    categories = dict(get_category_statistics())
    total = sum(categories.values())
    if total > 0:
        wants = categories.get("Vágy", 0)
        want_ratio = wants / total
        if want_ratio < 0.20:
            score += 10
        elif want_ratio > 0.50:
            score -= 10

    return max(0, min(100, int(score)))

def get_score_text(score):
    if score >= 90:
        return "Kiváló"
    if score >= 75:
        return "Nagyon jó"
    if score >= 60:
        return "Jó"
    if score >= 40:
        return "Fejleszthető"
    return "Figyelmet igényel"

def generate_advice():
    income, expense = get_month_statistics()
    advice = []

    if income == 0 and expense == 0:
        advice.append("Még nincs elegendő adatod a pénzügyi elemzéshez.")
        return advice

    savings = income - expense
    if income > 0:
        rate = savings / income
        if rate >= 0.20:
            advice.append("Remek a megtakarítás arányod!")
        else:
            advice.append(" Érdemes lenne növelni a havi megtakarításaidat.")

    months, _, _ = get_machine_learning_predictions()
    if len(months) >= 2:
        advice.append(
            f" A scikit-learn gépi tanuló modell sikeresen optimalizálta "
            f"a paramétereit {len(months)} hónap adatai alapján."
        )

    return advice

PLUGIN_DIR = Path("generated_charts")
PLUGIN_DIR.mkdir(exist_ok=True)

ALLOWED_AX_METHODS = {
    "plot", "scatter", "bar", "pie", "set_title", "set_xlabel",
    "set_ylabel", "legend", "grid", "tick_params", "set_xticks",
    "set_xticklabels", "set_yticks", "set_yticklabels",
    "get_xticks", "get_xticklabels", "get_yticks", "get_yticklabels",
    "text", "axhline", "axvline", "barh", "invert_yaxis",
    "set_axisbelow", "margins", "annotate", "set_ylim", "set_xlim",
    "get_ylim", "get_xlim", "set_facecolor", "set_frame_on",
    "set_axis_off", "set_axis_on", "set_aspect", "set_box_aspect",
    "set_visible"
}

ALLOWED_BUILTINS = {
    "sum", "len", "max", "min", "sorted", "enumerate", "round",
    "str", "float", "int", "list", "dict", "range", "abs"
}

ALLOWED_DATA_METHODS = {"keys", "values", "items", "get", "append", "extend", "sort", "count", "index"}

ALLOWED_AST_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.AugAssign,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.Call, ast.Attribute,
    ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.For, ast.If,
    ast.Expr, ast.Return, ast.Compare, ast.BinOp, ast.UnaryOp, ast.Add,
    ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.USub, ast.UAdd, ast.Eq,
    ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.ListComp, ast.comprehension, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.Not,
    ast.Continue, ast.Break, ast.keyword, ast.Lambda
)

def validate_generated_chart(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"A generált kód szintaktikailag hibás: {exc}")

    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("A kódnak egyetlen draw(ax, transactions, months, predicted, actual) függvényt kell tartalmaznia.")
    
    fn = tree.body[0]
    expected_args = ["ax", "transactions", "months", "predicted", "actual"]
    if fn.name != "draw" or [arg.arg for arg in fn.args.args] != expected_args:
        raise ValueError("A függvény aláírása nem megfelelő.")
    if fn.decorator_list or fn.args.vararg or fn.args.kwarg or fn.args.defaults:
        raise ValueError("Dekorátorok és változó argumentumok nem engedélyezettek.")

    forbidden_names = {
        "__import__", "eval", "exec", "compile", "open", "input",
        "globals", "locals", "getattr", "setattr", "delattr", "vars",
        "breakpoint", "help", "dir", "memoryview", "classmethod",
        "staticmethod", "super", "type", "object"
    }

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_AST_NODES):
            raise ValueError(f"Nem engedélyezett kódelem: {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in forbidden_names:
                raise ValueError(f"Nem engedélyezett név: {node.id}")
        if isinstance(node, ast.Attribute):
            is_axis_method = (
                isinstance(node.value, ast.Name)
                and node.value.id == "ax"
                and node.attr in ALLOWED_AX_METHODS
            )
            is_data_method = (
                isinstance(node.value, ast.Name)
                and node.value.id != "ax"
                and node.attr in ALLOWED_DATA_METHODS
            )
            if not (is_axis_method or is_data_method):
                raise ValueError(f"Nem engedélyezett attribútum vagy metódus: {node.attr}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in ALLOWED_BUILTINS:
                    raise ValueError(f"Nem engedélyezett függvényhívás: {node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                is_axis_method = (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ax"
                    and node.func.attr in ALLOWED_AX_METHODS
                )
                is_data_method = (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id != "ax"
                    and node.func.attr in ALLOWED_DATA_METHODS
                )
                if not (is_axis_method or is_data_method):
                    raise ValueError(f"Nem engedélyezett metódushívás: {node.func.attr}")
            else:
                raise ValueError("Nem engedélyezett függvényhívás.")
                
    return compile(tree, "<ai-generated-chart>", "exec")

def _safe_range(*args):
    values = range(*args)
    if len(values) > 10000:
        raise ValueError("A generált grafikon ciklusa túllépi a megengedett méretet.")
    return values

def load_generated_chart(source):
    code = validate_generated_chart(source)
    safe_builtins = {name: getattr(__builtins__, name) for name in ALLOWED_BUILTINS} if not isinstance(__builtins__, dict) else {name: __builtins__[name] for name in ALLOWED_BUILTINS}
    safe_builtins["range"] = _safe_range
    namespace = {"__builtins__": safe_builtins}
    exec(code, namespace, namespace)
    return namespace["draw"]

def generate_chart_code(request, previous_error=None):
    if ai_client is None:
        raise RuntimeError("Az MI nincs beállítva. Állítsd be a GEMINI_API_KEY környezeti változót, és telepítsd a google-genai csomagot.")

    prompt = f"""Feladat: írj Python kódot egy pénzügyi alkalmazás egyedi grafikonjához.
A felhasználó kérése: {request!r}

{("Az előző próbálkozás hibája: " + str(previous_error) + ". Javítsd ezt a hibát; kizárólag engedélyezett AST-elemeket és ax-metódusokat használj. Ne ismételd meg ugyanazt a hibát.") if previous_error else ""}

KIZÁRÓLAG ezt az egy függvényt add vissza, markdown kerítés és magyarázat nélkül:
def draw(ax, transactions, months, predicted, actual):
    ...

A függvény a matplotlib Axes objektumot az ax változóban kapja.
A transactions lista elemei: (id, tipus, osszeg, leiras, kat, datum).
A months havi címkék listája, predicted az illesztett ML trend, actual a havi valós kiadások listája.
A matplotlib rajzoló ax alábbi metódusait használhatod: plot, scatter, bar, pie, set_title, set_xlabel, set_ylabel, legend, grid, tick_params, set_xticks, set_xticklabels, set_yticks, set_yticklabels, get_xticks, get_xticklabels, get_yticks, get_yticklabels, text, axhline, axvline, barh, invert_yaxis, set_axisbelow, margins, annotate, set_ylim, set_xlim, get_ylim, get_xlim, set_facecolor, set_frame_on, set_axis_off, set_axis_on, set_aspect, set_box_aspect, set_visible.
Beépített függvények: sum, len, max, min, sorted, enumerate, round, str, float, int, list, dict, range, abs.
Használhatsz tetszőleges beszédes helyi változóneveket (például category_totals, category_names, category_values), listát, szótárat, ciklust, feltételt és egyszerű számításokat. Rendezéshez használhatod a sorted függvényt reverse=True paraméterrel; lambda kifejezést ne használj.
TILOS import, fájlkezelés, hálózat, adatbázis, os, eval, exec, lambda, while, try/except, osztály, globális változó vagy bármilyen más függvény/metódus.
A transactions adatai a valódi adatbázisból érkeznek. Pénzügyi tranzakciók esetén mindig a transactions listából dolgozz, ne a months/predicted/actual listákból következtess arra, hogy vannak-e tranzakciók. A tranzakció típusa pontosan „Kiadás” vagy „Bevétel”; a kiadásokhoz row[1] == "Kiadás" feltételt használj. A kategória row[4], az összeg row[2], a dátum row[5]. Ne írj ki „nincs rögzített adat” üzenetet, ha a transactions listában megfelelő rekordok vannak. Ha tényleg nincs megfelelő adat, rajzolj értelmes üresállapot-címet. A függvény kezelje az üres adatokat is, és adjon értelmes címet a grafikonnak. Ne használj set_frame vagy set_frame_on metódust, hacsak a felhasználó kérése nem igényli; a grafikon alapértelmezett kerete megfelelő."""
    
    response = ai_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )
    source = (response.text or "").strip()
    source = re.sub(r"^```(?:python)?\s*", "", source)
    source = re.sub(r"\s*```$", "", source).strip()
    if not source:
        raise ValueError("Az MI nem adott vissza kódot.")
    return source

def add_transaction(tipus, amount, description, category):
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            INSERT INTO penz
            (tipus, osszeg, leiras, kat, datum)
            VALUES (?, ?, ?, ?, ?)
        """, (
            tipus,
            amount,
            description,
            category,
            datetime.now().strftime("%Y-%m-%d")
        ))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def create_goal(name, target):
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            INSERT INTO celok
            (nev, celo, felretett, letrehozva)
            VALUES (?, ?, 0, ?)
        """, (name, target, datetime.now().strftime("%Y-%m-%d")))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def save_to_goal(goal_id, amount):
    connection = get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE celok SET felretett = felretett + ? WHERE id=?", (amount, goal_id))
        connection.execute("""
            INSERT INTO penz (tipus, osszeg, leiras, kat, datum)
            VALUES (?, ?, ?, ?, ?)
        """, ("Kiadás", amount, "Cél megtakarítás", "Megtakarítás", datetime.now().strftime("%Y-%m-%d")))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def delete_goal(goal_id):
    connection = get_connection()
    try:
        connection.execute("DELETE FROM celok WHERE id=?", (goal_id,))
        connection.commit()
    finally:
        connection.close()

class FinanceApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Pénzügyi Önedző – MI-alapú dinamikus bővítéssel")
        self.geometry("1250x820")
        self.minsize(1100, 720)

        self.chart_canvas = None
        self.generated_chart_functions = {}
        self.load_saved_chart_plugins()
        self.setup_ui()

    def setup_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=230, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        ctk.CTkLabel(
            self.sidebar,
            text="💰 PÉNZÜGYI\nÖNEDZŐ",
            font=("Arial", 25, "bold")
        ).pack(pady=(35, 40))

        ctk.CTkButton(
            self.sidebar,
            text="🏠  Dashboard",
            command=lambda: self.show_page("dashboard")
        ).pack(padx=20, pady=7, fill="x")

        ctk.CTkButton(
            self.sidebar,
            text="💳  Tranzakciók",
            command=lambda: self.show_page("transactions")
        ).pack(padx=20, pady=7, fill="x")

        ctk.CTkButton(
            self.sidebar,
            text="🎯  Célok",
            command=lambda: self.show_page("goals")
        ).pack(padx=20, pady=7, fill="x")

        ctk.CTkButton(
            self.sidebar,
            text="🧠  Önelemzés",
            command=lambda: self.show_page("analysis")
        ).pack(padx=20, pady=7, fill="x")

        ctk.CTkButton(
            self.sidebar,
            text="📊  Grafikonok",
            command=lambda: self.show_page("charts")
        ).pack(padx=20, pady=7, fill="x")

        self.theme_button = ctk.CTkButton(
            self.sidebar,
            text="☀️  Világos mód",
            command=self.toggle_theme
        )
        self.theme_button.pack(side="bottom", padx=20, pady=25, fill="x")

        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main.grid_rowconfigure(0, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        self.show_page("dashboard")

    def clear_main(self):
        for widget in self.main.winfo_children():
            widget.destroy()

    def show_page(self, page):
        self.clear_main()
        if page == "dashboard":
            self.dashboard_page()
        elif page == "transactions":
            self.transactions_page()
        elif page == "goals":
            self.goals_page()
        elif page == "analysis":
            self.analysis_page()
        elif page == "charts":
            self.charts_page()

    def dashboard_page(self):
        ctk.CTkLabel(self.main, text="Jó napot! 👋", font=("Arial", 30, "bold")).pack(anchor="w", pady=(5, 2))
        
        balance = get_balance()
        income, expense = get_month_statistics()
        score = calculate_score()

        cards = ctk.CTkFrame(self.main, fg_color="transparent")
        cards.pack(fill="x", pady=10)
        
        self.card(cards, "💰 Egyenleg", money(balance), 0)
        self.card(cards, "📥 Havi bevétel", money(income), 1)
        self.card(cards, "📤 Havi kiadás", money(expense), 2)
        self.card(cards, "🧠 Pontszám", f"{score}/100", 3)

        advice_frame = ctk.CTkFrame(self.main)
        advice_frame.pack(fill="both", expand=True, pady=10)

        ctk.CTkLabel(advice_frame, text="🧠 Gépi Tanulás (ML) Elemzés", font=("Arial", 21, "bold")).pack(anchor="w", padx=20, pady=(20, 10))
        for item in generate_advice():
            ctk.CTkLabel(advice_frame, text=item, justify="left").pack(anchor="w", padx=20, pady=5)

    def card(self, parent, title, value, column):
        frame = ctk.CTkFrame(parent, height=120)
        frame.grid(row=0, column=column, padx=6, sticky="nsew")
        parent.grid_columnconfigure(column, weight=1)
        ctk.CTkLabel(frame, text=title, font=("Arial", 14)).pack(anchor="w", padx=18, pady=(18, 5))
        ctk.CTkLabel(frame, text=value, font=("Arial", 23, "bold")).pack(anchor="w", padx=18)

    def transactions_page(self):
        ctk.CTkLabel(self.main, text="💳 Tranzakciók", font=("Arial", 30, "bold")).pack(anchor="w", pady=(5, 20))

        form = ctk.CTkFrame(self.main)
        form.pack(fill="x", pady=(0, 15))

        self.amount_entry = ctk.CTkEntry(form, placeholder_text="Összeg")
        self.amount_entry.grid(row=0, column=0, padx=10, pady=15)

        self.description_entry = ctk.CTkEntry(form, placeholder_text="Leírás")
        self.description_entry.grid(row=0, column=1, padx=10, pady=15)

        self.category_combo = ctk.CTkComboBox(form, values=["Szükséglet", "Vágy", "Élelmiszer", "Közlekedés", "Egyéb"])
        self.category_combo.set("Szükséglet")
        self.category_combo.grid(row=0, column=2, padx=10, pady=15)

        ctk.CTkButton(form, text="➕ Bevétel", command=lambda: self.save_trx("Bevétel")).grid(row=1, column=0, padx=10, pady=(0, 15))
        ctk.CTkButton(form, text="➖ Kiadás", command=lambda: self.save_trx("Kiadás")).grid(row=1, column=1, padx=10, pady=(0, 15))

        list_frame = ctk.CTkScrollableFrame(self.main)
        list_frame.pack(fill="both", expand=True)

        for _, t, amount, desc, category, date in get_transactions():
            row = ctk.CTkFrame(list_frame)
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=date, width=100).pack(side="left", padx=10)
            ctk.CTkLabel(row, text=desc or "-", width=220, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=("+" if t == "Bevétel" else "-") + money(amount)).pack(side="right", padx=15)

    def save_trx(self, t_type):
        try:
            amt = float(self.amount_entry.get().replace(" ", "").replace(",", "."))
            if amt <= 0: raise ValueError
        except:
            messagebox.showerror("Hiba", "Érvényes összeget adj meg!")
            return

        add_transaction(t_type, amt, self.description_entry.get().strip() or "Nincs leírás", self.category_combo.get())
        self.amount_entry.delete(0, "end")
        self.description_entry.delete(0, "end")
        self.show_page("transactions")

    def goals_page(self):
        ctk.CTkLabel(self.main, text="🎯 Célok", font=("Arial", 30, "bold")).pack(anchor="w", pady=(5, 20))
        create = ctk.CTkFrame(self.main)
        create.pack(fill="x", pady=(0, 20))

        self.goal_name = ctk.CTkEntry(create, placeholder_text="Cél neve")
        self.goal_name.grid(row=0, column=0, padx=10, pady=15)
        self.goal_amount = ctk.CTkEntry(create, placeholder_text="Cél összege")
        self.goal_amount.grid(row=0, column=1, padx=10, pady=15)

        ctk.CTkButton(create, text="Létrehozás", command=self.create_g_action).grid(row=0, column=2, padx=10)

        container = ctk.CTkScrollableFrame(self.main)
        container.pack(fill="both", expand=True)

        for gid, name, target, saved, _ in get_goals():
            fr = ctk.CTkFrame(container)
            fr.pack(fill="x", pady=8)
            ctk.CTkLabel(fr, text=f"{name} - {money(saved)} / {money(target)}", font=("Arial", 16, "bold")).pack(anchor="w", padx=20, pady=10)

    def create_g_action(self):
        name = self.goal_name.get().strip()
        try:
            target = float(self.goal_amount.get().replace(",", "."))
            if not name or target <= 0: raise ValueError
            create_goal(name, target)
            self.show_page("goals")
        except:
            messagebox.showerror("Hiba", "Helytelen adatok!")

    def analysis_page(self):
        ctk.CTkLabel(self.main, text="🧠 Önelemzés", font=("Arial", 30, "bold")).pack(anchor="w", pady=(5, 10))
        score = calculate_score()
        ctk.CTkLabel(self.main, text=f"Pontszám: {score}/100", font=("Arial", 22, "bold")).pack(anchor="w", pady=10)

    def load_saved_chart_plugins(self):
        for path in PLUGIN_DIR.glob("*.py"):
            try:
                source = path.read_text(encoding="utf-8")
                self.generated_chart_functions[path.stem] = load_generated_chart(source)
            except Exception as exc:
                print(f"Bővítmény kihagyva ({path.name}): {exc}")

    def request_custom_chart(self):
        request = self.custom_chart_entry.get().strip()
        if not request:
            messagebox.showinfo("Egyedi grafikon", "Írd le, milyen grafikont szeretnél!")
            return
        if ai_client is None:
            messagebox.showerror("MI nincs beállítva", "Állítsd be a GEMINI_API_KEY változót, és telepítsd: pip install google-genai")
            return

        self.custom_chart_button.configure(state="disabled", text="MI dolgozik...")
        self.update_idletasks()

        def worker():
            last_error = None
            for attempt in range(3):
                try:
                    if attempt:
                        time.sleep(attempt)
                    source = generate_chart_code(request, previous_error=last_error)
                    draw_fn = load_generated_chart(source)

                    real_transactions = get_transactions()
                    months, predicted, actual = get_machine_learning_predictions()
                    test_fig = Figure(figsize=(5, 3))
                    test_ax = test_fig.add_subplot(111)
                    draw_fn(test_ax, real_transactions, months, predicted, actual)
                    test_fig.clear()

                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", request.lower()).strip("_")[:32] or "egyedi_grafikon"
                    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}"
                    (PLUGIN_DIR / f"{filename}.py").write_text(source, encoding="utf-8")
                    self.generated_chart_functions[filename] = draw_fn
                    self.after(0, lambda name=filename: self._custom_chart_ready(name, request))
                    return
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            self.after(0, lambda error=last_error: self._custom_chart_failed(error))
        threading.Thread(target=worker, daemon=True).start()

    def _custom_chart_ready(self, plugin_name, request):
        self.custom_chart_button.configure(state="normal", text="✨ Grafikon létrehozása MI-vel")
        self.draw_chart(f"custom:{plugin_name}")
        messagebox.showinfo("Sikeres bővítés", f"Az MI létrehozta, ellenőrizte és elmentette az új grafikont.\n\nKérés: {request}\nBővítmény: {plugin_name}")

    def _custom_chart_failed(self, error):
        self.custom_chart_button.configure(state="normal", text="✨ Grafikon létrehozása MI-vel")
        messagebox.showerror("A grafikon nem készült el", f"Az MI által generált kód nem ment át az ellenőrzésen vagy a teszten.\nA meglévő alkalmazás változatlan maradt.\n\nRészlet: {error}")

    def charts_page(self):
        ctk.CTkLabel(self.main, text="📊 Gépi Tanulás Grafikonok", font=("Arial", 30, "bold")).pack(anchor="w", pady=(5, 15))

        buttons = ctk.CTkFrame(self.main, fg_color="transparent")
        buttons.pack(fill="x", pady=5)

        ctk.CTkButton(buttons, text="🤖 ML Trend & Predikció", command=lambda: self.draw_chart("ml_pred")).pack(side="left", padx=5)
        ctk.CTkButton(buttons, text="🥧 Kategóriák", command=lambda: self.draw_chart("category")).pack(side="left", padx=5)

        custom_box = ctk.CTkFrame(self.main)
        custom_box.pack(fill="x", pady=(10, 0))
        self.custom_chart_entry = ctk.CTkEntry(
            custom_box,
            placeholder_text="Pl. mutasd meg oszlopdiagramon a kategóriák kiadásait, csökkenő sorrendben",
            height=38
        )
        self.custom_chart_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.custom_chart_button = ctk.CTkButton(
            custom_box,
            text="✨ Grafikon létrehozása MI-vel",
            command=self.request_custom_chart
        )
        self.custom_chart_button.pack(side="right", padx=8, pady=8)

        self.chart_frame = ctk.CTkFrame(self.main)
        self.chart_frame.pack(fill="both", expand=True, pady=15)

        self.draw_chart("ml_pred")
        if self.generated_chart_functions:
            saved = ctk.CTkFrame(self.main, fg_color="transparent")
            saved.pack(fill="x", pady=(0, 5))
            ctk.CTkLabel(saved, text="Mentett MI-grafikonok:").pack(side="left", padx=5)
            for plugin_name in list(self.generated_chart_functions.keys())[-5:]:
                ctk.CTkButton(
                    saved, text=plugin_name[:22],
                    command=lambda name=plugin_name: self.draw_chart(f"custom:{name}")
                ).pack(side="left", padx=3)

    def draw_chart(self, chart_type):
        for widget in self.chart_frame.winfo_children():
            widget.destroy()

        fig = Figure(figsize=(8, 4.8), dpi=100)
        ax = fig.add_subplot(111)

        if chart_type == "ml_pred":
            months, predicted, actual = get_machine_learning_predictions()

            if not months:
                ctk.CTkLabel(self.chart_frame, text="Nincs elég adat az ML modell tanításához.").pack(pady=50)
                return

            ax.plot(months, predicted, marker="o", linestyle="--", color="#00ffcc", label="ML Model Trend (Optimalizált)", linewidth=2.5)
            ax.plot(months, actual, marker="s", linestyle="-", color="#ff6b6b", label="Valós kiadások", linewidth=2.5)

            ax.set_title("Scikit-Learn Lineáris Regressziós AI Predikció")
            ax.set_ylabel("Ft")
            ax.legend(loc="upper left")
            ax.grid(True, linestyle=":", alpha=0.6)
            ax.tick_params(axis="x", rotation=25)

        elif chart_type == "category":
            categories = defaultdict(float)
            for _, t, amount, _, category, _ in get_transactions():
                if t == "Kiadás":
                    categories[category or "Egyéb"] += amount
            if categories:
                ax.pie(list(categories.values()), labels=list(categories.keys()), autopct="%1.1f%%")
                ax.set_title("Kiadások kategóriák szerint")

        elif chart_type.startswith("custom:"):
            plugin_name = chart_type.split(":", 1)[1]
            draw_fn = self.generated_chart_functions.get(plugin_name)
            if draw_fn is None:
                ctk.CTkLabel(self.chart_frame, text="A mentett grafikonbővítmény nem tölthető be.").pack(pady=50)
                return
            months, predicted, actual = get_machine_learning_predictions()
            draw_fn(ax, get_transactions(), months, predicted, actual)

        fig.tight_layout()
        self.chart_canvas = FigureCanvasTkAgg(fig, self.chart_frame)
        self.chart_canvas.draw()
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)

    def toggle_theme(self):
        if ctk.get_appearance_mode() == "Dark":
            ctk.set_appearance_mode("light")
        else:
            ctk.set_appearance_mode("dark")

if __name__ == "__main__":
    initialize_database()
    app = FinanceApp()
    app.mainloop()