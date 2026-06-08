"""
GE H80-200 — диагностика лопатки СА по данным ANSYS CFD.

Версия 4.0:
- эталонные параметры берутся при RA_REFERENCE, а не из первой строки CSV;
- CSV читается штатным csv-модулем, без ручного split;
- дубли Ra агрегируются средним значением;
- Ra валидируется по фактическому диапазону CFD-таблицы;
- отрицательная Delta N не увеличивает DI как деградация;
- расчеты вынесены из UI в отдельные dataclass/классы;
- ошибки ввода в экономике показываются пользователю.
"""

from __future__ import annotations

import csv
import math
import os
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import matplotlib

matplotlib.use("TkAgg")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

try:
    from scipy.interpolate import PchipInterpolator
except ImportError:  # приложение остается работоспособным без SciPy
    PchipInterpolator = None


import sys as _sys
# В frozen-бандле (PyInstaller) файлы данных распаковываются в sys._MEIPASS
SCRIPT_DIR = getattr(_sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
LOGO_PATH = os.path.join(SCRIPT_DIR, "mgtu_logo.png")


RA_REFERENCE = 5.0
RA_LIMIT_WARN = 50.0
RA_LIMIT_CRIT = 85.0
ZETA_LIMIT_FACTOR = 1.10  # порог ζ = эталонное ζ (при ref_ra) × коэффициент

STD_PRESSURE_KPA = 101.325
STD_TEMP_K = 288.15
HP_TO_KW = 0.73549875

PARAM_MAP = {
    "P2": ("Шероховатость Ra", "мкм", 4),
    "P1": ("Масс. расход", "кг/с", 6),
    "P4": ("Полное давление вых.", "кПа", 2),
    "P5": ("Полное давление вх.", "кПа", 2),
    "P6": ("Статич. давление вых.", "кПа", 2),
    "P7": ("Коэфф. потерь Zeta", "-", 6),
    "P8": ("Число Маха (выход)", "-", 5),
    "P9": ("Угол потока Alpha1", "град", 3),
}

MODEL_CODES = ("P1", "P4", "P5", "P6", "P7", "P8", "P9")
DI_WEIGHTS = {"P7": 0.50, "P1": 0.30, "P8": 0.15, "P9": 0.05}


class C:
    BG_APP = "#eef2f8"
    BG_CARD = "#ffffff"
    BG_HEADER = "#ffffff"
    BG_SIDEBAR = "#0d1b3e"
    BG_SIDEBAR_2 = "#152548"
    BG_INPUT = "#f5f8fd"

    BORDER = "#dde3ef"
    BORDER_DK = "#b8c5d9"

    TEXT_DARK = "#0d1b3e"
    TEXT = "#2d4160"
    TEXT_MUTED = "#5c7394"
    TEXT_FAINT = "#8ea8c3"
    TEXT_WHITE = "#ffffff"
    TEXT_SB = "#dde8f8"
    TEXT_SB_DIM = "#7a9abf"

    PRIMARY = "#1e4592"
    PRIMARY_DK = "#163a82"
    PRIMARY_LT = "#2d5fbe"

    SEC_BG = "#dde3ef"
    SEC_HOVER = "#b8c5d9"
    SEC_FG = "#0d1b3e"

    SUCCESS = "#10b981"
    SUCCESS_BG = "#d1fae5"
    WARNING = "#f59e0b"
    WARNING_BG = "#fef3c7"
    DANGER = "#ef4444"
    DANGER_BG = "#fee2e2"
    INFO = "#0891b2"
    INFO_BG = "#cffafe"
    NEUTRAL = "#5c7394"
    NEUTRAL_BG = "#dde3ef"
    CHART_2 = "#10b981"

    UNIT_BTN = "#1e3a80"
    UNIT_BTN_HOV = "#2d5fbe"
    UNIT_FG = "#93c5fd"


def setup_mpl_style() -> None:
    plt.rcParams.update(
        {
            "font.family": ["Segoe UI", "Arial", "sans-serif"],
            "font.size": 9,
            "axes.edgecolor": C.BORDER_DK,
            "axes.linewidth": 0.8,
            "axes.labelcolor": C.TEXT,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlecolor": C.TEXT_DARK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": C.TEXT_MUTED,
            "ytick.color": C.TEXT_MUTED,
            "grid.color": C.BORDER,
            "grid.linestyle": "--",
            "grid.linewidth": 0.6,
            "grid.alpha": 0.7,
            "legend.frameon": False,
            "legend.fontsize": 7.5,
            "figure.facecolor": C.BG_CARD,
            "axes.facecolor": C.BG_CARD,
        }
    )


def to_kpa(value: float, unit: str) -> float:
    return {"кПа": value, "бар": value * 100.0, "psi": value * 6.89476, "атм": value * 101.325}.get(unit, value)


def from_kpa(value: float, unit: str) -> float:
    return {"кПа": value, "бар": value / 100.0, "psi": value / 6.89476, "атм": value / 101.325}.get(unit, value)


def to_k(value: float, unit: str) -> float:
    return value + 273.15 if unit == "°C" else value


def from_k(value: float, unit: str) -> float:
    return value - 273.15 if unit == "°C" else value


def to_kw(value: float, unit: str) -> float:
    return value * HP_TO_KW if unit == "л.с." else value


def from_kw(value: float, unit: str) -> float:
    return value / HP_TO_KW if unit == "л.с." else value


def fmt_num(value: float, digits: int = 5) -> str:
    return f"{value:.{digits}g}"


def parse_float(text: str, field_name: str) -> float:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Поле «{field_name}» должно быть числом.") from exc


def detect_csv_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        return dialect.delimiter
    except csv.Error:
        return ";" if sample.count(";") >= sample.count(",") else ","


def load_logo(size: int) -> tk.PhotoImage | None:
    if not os.path.exists(LOGO_PATH):
        return None
    try:
        from PIL import Image, ImageTk

        img = Image.open(LOGO_PATH).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except ImportError:
        pass
    except OSError:
        return None

    try:
        raw = tk.PhotoImage(file=LOGO_PATH)
        factor = max(raw.width() // size, raw.height() // size, 1)
        return raw.subsample(factor) if factor > 1 else raw
    except tk.TclError:
        return None


@dataclass(frozen=True)
class StandInputs:
    ra: float
    pressure_kpa: float
    temp_k: float
    measured_kw: float
    nominal_kw: float
    pressure_display: float
    temp_display: float
    measured_display: float
    nominal_display: float
    pressure_unit: str
    temp_unit: str
    power_unit: str


@dataclass(frozen=True)
class AnalysisResult:
    ra: float
    pressure_kpa: float
    temp_k: float
    measured_kw: float
    nominal_kw: float
    pressure_display: float
    temp_display: float
    measured_display: float
    nominal_display: float
    pressure_unit: str
    temp_unit: str
    power_unit: str
    delta: float
    theta: float
    reduced_kw: float
    expected_kw: float
    flow_ref: float
    flow_now: float
    flow_ratio: float
    power_dev: float
    zeta_ref: float
    zeta_now: float
    zeta_deg: float
    flow_loss: float
    di: float
    status: str
    status_sub: str
    status_tag: str
    accent: str
    ra_threshold: float | None
    ref_ra: float


class Card(tk.Frame):
    def __init__(self, master: tk.Widget, **kwargs) -> None:
        super().__init__(
            master,
            bg=C.BG_CARD,
            highlightbackground=C.BORDER,
            highlightthickness=1,
            bd=0,
            **kwargs,
        )


class CfdModel:
    def __init__(self, points: list[dict[str, float | str]]) -> None:
        if len(points) < 2:
            raise ValueError("Нужно минимум 2 валидные CFD-точки.")

        self.points = self._deduplicate(points)
        if len(self.points) < 2:
            raise ValueError("После удаления дублей Ra осталось меньше 2 точек.")

        self.ra = np.array([float(p["P2"]) for p in self.points], dtype=float)
        self.ra_min = float(self.ra.min())
        self.ra_max = float(self.ra.max())
        self.interp: dict[str, Callable[[float | np.ndarray], float | np.ndarray]] = {}
        self.ref_ra = RA_REFERENCE if self.ra_min <= RA_REFERENCE <= self.ra_max else self.ra_min
        self.ref_params: dict[str, float] = {}
        self.param_ranges: dict[str, float] = {}

        self._build_interpolators()
        self._build_reference()
        self._build_ranges()
        self.zeta_limit: float | None = (
            self.ref_params["P7"] * ZETA_LIMIT_FACTOR if "P7" in self.ref_params else None
        )

    @staticmethod
    def _deduplicate(points: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
        grouped: dict[float, list[dict[str, float | str]]] = {}
        for point in points:
            grouped.setdefault(float(point["P2"]), []).append(point)

        result: list[dict[str, float | str]] = []
        for ra in sorted(grouped):
            rows = grouped[ra]
            merged: dict[str, float | str] = {"Name": rows[0].get("Name", f"Ra {ra:g}"), "P2": ra}
            for code in MODEL_CODES:
                vals = [float(row[code]) for row in rows if code in row and row[code] != ""]
                if vals:
                    merged[code] = float(np.mean(vals))
            result.append(merged)
        return result

    def _build_interpolators(self) -> None:
        for code in MODEL_CODES:
            vals = []
            for point in self.points:
                if code not in point:
                    vals = []
                    break
                vals.append(float(point[code]))
            if not vals:
                continue

            y = np.array(vals, dtype=float)
            if PchipInterpolator is not None and len(self.ra) >= 3:
                interpolator = PchipInterpolator(self.ra, y, extrapolate=False)

                def fn(x, interp=interpolator, y_values=y):
                    arr = np.asarray(x)
                    clipped = np.clip(arr, self.ra_min, self.ra_max)
                    out = interp(clipped)
                    if np.isscalar(x):
                        return float(out)
                    return out

                self.interp[code] = fn
            else:

                def fn(x, y_values=y):
                    arr = np.asarray(x)
                    out = np.interp(arr, self.ra, y_values)
                    if np.isscalar(x):
                        return float(out)
                    return out

                self.interp[code] = fn

    def _build_reference(self) -> None:
        for code in ("P1", "P4", "P7", "P8", "P9"):
            if code in self.interp:
                self.ref_params[code] = float(self.interp[code](self.ref_ra))

    def _build_ranges(self) -> None:
        for code in DI_WEIGHTS:
            if code not in self.ref_params:
                continue
            ref = self.ref_params[code]
            vals = [float(point[code]) for point in self.points if code in point]
            max_dev = max(abs(value - ref) for value in vals) if vals else 0.0
            self.param_ranges[code] = max(max_dev, 1e-10)

    def value(self, code: str, ra: float) -> float | None:
        if code not in self.interp:
            return None
        return float(self.interp[code](ra))

    def calc_di(self, ra: float, power_dev: float = 0.0) -> float:
        di_power = min(max(power_dev, 0.0) / 10.0, 1.0) * 100.0
        di_aero = 0.0
        for code, weight in DI_WEIGHTS.items():
            if code not in self.interp or code not in self.param_ranges:
                continue
            ref = self.ref_params.get(code)
            cur = self.value(code, ra)
            if ref is None or cur is None:
                continue
            norm = min(abs(cur - ref) / self.param_ranges[code], 1.0)
            di_aero += weight * norm * 100.0
        return 0.60 * di_power + 0.40 * di_aero

    def estimate_ra_threshold(self) -> float | None:
        if "P7" not in self.interp or self.zeta_limit is None:
            return None
        for ra in np.linspace(self.ra_min, self.ra_max, 500):
            zeta = self.value("P7", float(ra))
            if zeta is not None and zeta > self.zeta_limit:
                return float(ra)
        return None

    def analyze(self, inputs: StandInputs) -> AnalysisResult:
        if not self.ra_min <= inputs.ra <= self.ra_max:
            raise ValueError(
                f"Ra = {inputs.ra:.2f} мкм вне диапазона CFD-таблицы: "
                f"{self.ra_min:.2f}–{self.ra_max:.2f} мкм."
            )
        if inputs.pressure_kpa <= 0 or inputs.temp_k <= 0:
            raise ValueError("Давление и температура должны быть положительными.")
        if inputs.measured_kw <= 0 or inputs.nominal_kw <= 0:
            raise ValueError("Мощность должна быть положительной.")

        delta = inputs.pressure_kpa / STD_PRESSURE_KPA
        theta = inputs.temp_k / STD_TEMP_K
        denom = delta * math.sqrt(theta)
        if denom <= 0:
            raise ValueError("Некорректные атмосферные условия.")

        reduced_kw = inputs.measured_kw / denom
        flow_ref = self.ref_params.get("P1", 1.0) or 1.0
        flow_now = self.value("P1", inputs.ra) or flow_ref
        flow_ratio = flow_now / flow_ref if flow_ref else 1.0
        expected_kw = inputs.nominal_kw * flow_ratio
        power_dev = (expected_kw - reduced_kw) / expected_kw * 100.0 if expected_kw > 0 else 0.0

        zeta_ref = self.ref_params.get("P7", 0.065) or 0.065
        zeta_now = self.value("P7", inputs.ra) or zeta_ref
        zeta_deg = (zeta_now - zeta_ref) / zeta_ref * 100.0 if zeta_ref else 0.0
        flow_loss = (flow_ref - flow_now) / flow_ref * 100.0 if flow_ref else 0.0
        di = self.calc_di(inputs.ra, power_dev)

        status, accent, tag, status_sub = classify_status(inputs.ra, power_dev, di)

        return AnalysisResult(
            ra=inputs.ra,
            pressure_kpa=inputs.pressure_kpa,
            temp_k=inputs.temp_k,
            measured_kw=inputs.measured_kw,
            nominal_kw=inputs.nominal_kw,
            pressure_display=inputs.pressure_display,
            temp_display=inputs.temp_display,
            measured_display=inputs.measured_display,
            nominal_display=inputs.nominal_display,
            pressure_unit=inputs.pressure_unit,
            temp_unit=inputs.temp_unit,
            power_unit=inputs.power_unit,
            delta=delta,
            theta=theta,
            reduced_kw=reduced_kw,
            expected_kw=expected_kw,
            flow_ref=flow_ref,
            flow_now=flow_now,
            flow_ratio=flow_ratio,
            power_dev=power_dev,
            zeta_ref=zeta_ref,
            zeta_now=zeta_now,
            zeta_deg=zeta_deg,
            flow_loss=flow_loss,
            di=di,
            status=status,
            status_sub=status_sub,
            status_tag=tag,
            accent=accent,
            ra_threshold=self.estimate_ra_threshold(),
            ref_ra=self.ref_ra,
        )


def classify_status(ra: float, power_dev: float, di: float) -> tuple[str, str, str, str]:
    if power_dev > 10.0 or ra > RA_LIMIT_CRIT or di >= 70.0:
        return "КРИТИЧНО", C.DANGER, "danger", "требуется ремонт"
    if power_dev > 5.0 or ra > RA_LIMIT_WARN or di >= 30.0:
        return "ПРЕДУПРЕЖДЕНИЕ", C.WARNING, "warn", "нужна проверка"
    if power_dev > 2.0 or ra > RA_REFERENCE * 1.5:
        return "КОНТРОЛЬ", "#eab308", "warn", "в пределах нормы"
    return "НОРМА", C.SUCCESS, "success", "соответствует модели"


def read_ansys_csv(path: str) -> list[dict[str, float | str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        text = file.read()

    lines = text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if "Name" in line and "P2" in line), -1)
    if header_idx < 0:
        raise ValueError("Строка заголовка с колонками Name и P2 не найдена.")

    sample = "\n".join(lines[header_idx : header_idx + 5])
    delimiter = detect_csv_delimiter(sample)
    reader = csv.DictReader(lines[header_idx:], delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("Не удалось прочитать заголовок CSV.")

    fieldnames = [name.strip() for name in reader.fieldnames]
    reader.fieldnames = fieldnames
    if "P2" not in fieldnames or "P1" not in fieldnames:
        raise ValueError("CSV должен содержать минимум колонки P2 и P1.")

    points: list[dict[str, float | str]] = []
    for row in reader:
        if not row:
            continue
        cleaned: dict[str, float | str] = {}
        for key, value in row.items():
            if key is None:
                continue
            key = key.strip()
            text_value = (value or "").strip()
            if not text_value:
                continue
            if key == "Name":
                cleaned[key] = text_value
            else:
                try:
                    cleaned[key] = float(text_value.replace(",", "."))
                except ValueError:
                    pass

        if "P2" in cleaned and "P1" in cleaned:
            points.append(cleaned)

    if len(points) < 2:
        raise ValueError("Нужно минимум 2 валидные строки с числовыми P2 и P1.")
    return points


class DiagnosticApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GE H80-200 • Диагностика лопатки СА | МГТУ ГА")
        self.root.geometry("1480x920")
        self.root.minsize(1260, 820)
        self.root.configure(bg=C.BG_APP)

        self.model: CfdModel | None = None
        self.last: AnalysisResult | None = None
        self.csv_path: str | None = None

        self.entries: dict[str, tk.Entry] = {}
        self.econ_entries: dict[str, tk.Entry] = {}
        self.fig_trends: Figure | None = None
        self.fig_di: Figure | None = None
        self.fig_econ: Figure | None = None

        self.unit_pressure = tk.StringVar(value="кПа")
        self.unit_temp = tk.StringVar(value="К")
        self.unit_power = tk.StringVar(value="кВт")
        self.prev_pressure_unit = "кПа"
        self.prev_temp_unit = "К"
        self.prev_power_unit = "кВт"

        self.logo_img = load_logo(46)
        setup_mpl_style()
        self._setup_style()
        self._build_ui()

    def _setup_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Modern.TNotebook", background=C.BG_APP, borderwidth=0)
        style.configure(
            "Modern.TNotebook.Tab",
            padding=(18, 8),
            font=("Segoe UI", 10),
            background=C.BG_APP,
            foreground=C.TEXT_MUTED,
            borderwidth=0,
        )
        style.map(
            "Modern.TNotebook.Tab",
            background=[("selected", C.BG_CARD)],
            foreground=[("selected", C.PRIMARY)],
            expand=[("selected", [1, 1, 1, 0])],
        )

        style.configure(
            "Modern.Treeview",
            background=C.BG_CARD,
            fieldbackground=C.BG_CARD,
            foreground=C.TEXT,
            rowheight=28,
            font=("Segoe UI", 9),
            borderwidth=0,
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=C.BG_INPUT,
            foreground=C.TEXT_DARK,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padding=(8, 8),
        )
        style.map("Modern.Treeview", background=[("selected", C.INFO_BG)], foreground=[("selected", C.TEXT_DARK)])
        style.configure("Modern.Vertical.TScrollbar", background=C.BG_APP, troughcolor=C.BG_CARD, borderwidth=0)
        style.configure("Modern.Horizontal.TScrollbar", background=C.BG_APP, troughcolor=C.BG_CARD, borderwidth=0)

    @staticmethod
    def _btn(master: tk.Widget, text: str, command: Callable[[], None], *, kind: str = "primary", width: int | None = None) -> tk.Button:
        palette = {
            "primary": (C.PRIMARY_LT, C.PRIMARY, C.TEXT_WHITE),
            "secondary": (C.SEC_BG, C.SEC_HOVER, C.SEC_FG),
            "dark": ("#2d4160", C.BG_SIDEBAR, C.TEXT_WHITE),
            "success": (C.SUCCESS, "#059669", C.TEXT_WHITE),
            "ghost": ("#1e3560", "#2d5080", C.TEXT_SB),
        }
        bg, hover, fg = palette[kind]
        button = tk.Button(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            padx=16,
            pady=10,
        )
        if width is not None:
            button.configure(width=width)
        button.bind("<Enter>", lambda _: button.configure(bg=hover))
        button.bind("<Leave>", lambda _: button.configure(bg=bg))
        return button

    def _build_ui(self) -> None:
        self._build_header()
        body = tk.Frame(self.root, bg=C.BG_APP)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_sidebar(body)
        self._build_main(body)

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=C.BG_HEADER, height=68, highlightbackground=C.BORDER, highlightthickness=1)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        tk.Frame(header, bg=C.PRIMARY, height=3).pack(fill=tk.X, side=tk.BOTTOM)

        left = tk.Frame(header, bg=C.BG_HEADER)
        left.pack(side=tk.LEFT, padx=20, pady=6)
        if self.logo_img:
            tk.Label(left, image=self.logo_img, bg=C.BG_HEADER).pack(side=tk.LEFT, padx=(0, 14))
        else:
            tk.Label(left, text="GE", font=("Segoe UI", 18, "bold"), bg=C.BG_HEADER, fg=C.PRIMARY).pack(side=tk.LEFT, padx=(0, 14))

        tk.Frame(left, bg=C.BORDER_DK, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 14))
        title_box = tk.Frame(left, bg=C.BG_HEADER)
        title_box.pack(side=tk.LEFT)
        tk.Label(
            title_box,
            text="GE H80-200 • Диагностика лопатки СА",
            font=("Segoe UI", 13, "bold"),
            bg=C.BG_HEADER,
            fg=C.TEXT_DARK,
        ).pack(anchor=tk.W)
        tk.Label(
            title_box,
            text="Идентификация технического состояния по данным ANSYS CFD | МГТУ ГА",
            font=("Segoe UI", 9),
            bg=C.BG_HEADER,
            fg=C.TEXT_MUTED,
        ).pack(anchor=tk.W)

        right = tk.Frame(header, bg=C.BG_HEADER)
        right.pack(side=tk.RIGHT, padx=22, pady=10)
        self._btn(right, "Загрузить CSV", self.load_csv, kind="secondary").pack(side=tk.LEFT, padx=4)
        self._btn(right, "Экспорт отчёта", self.export_report, kind="secondary").pack(side=tk.LEFT, padx=4)

    def _build_sidebar(self, parent: tk.Widget) -> None:
        sidebar = tk.Frame(parent, bg=C.BG_SIDEBAR, width=330)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        status = tk.Frame(sidebar, bg=C.BG_SIDEBAR_2)
        status.pack(fill=tk.X, padx=16, pady=(18, 10))
        tk.Label(status, text="ДАННЫЕ ANSYS", font=("Segoe UI", 8, "bold"), bg=C.BG_SIDEBAR_2, fg=C.TEXT_SB_DIM).pack(
            anchor=tk.W, padx=12, pady=(10, 2)
        )
        self.csv_status_lbl = tk.Label(status, text="● Файл не загружен", font=("Segoe UI", 10, "bold"), bg=C.BG_SIDEBAR_2, fg="#fb923c")
        self.csv_status_lbl.pack(anchor=tk.W, padx=12, pady=(0, 2))
        self.csv_meta_lbl = tk.Label(
            status,
            text="Нажмите «Загрузить CSV» для начала",
            font=("Segoe UI", 8),
            bg=C.BG_SIDEBAR_2,
            fg=C.TEXT_SB_DIM,
            wraplength=270,
            justify=tk.LEFT,
        )
        self.csv_meta_lbl.pack(anchor=tk.W, padx=12, pady=(0, 10))

        tk.Label(sidebar, text="СТЕНДОВЫЕ ИЗМЕРЕНИЯ", font=("Segoe UI", 8, "bold"), bg=C.BG_SIDEBAR, fg=C.TEXT_SB_DIM).pack(
            anchor=tk.W, padx=28, pady=(18, 8)
        )

        fields = [
            ("ra_meas", "Ra после ремонта", "10.0", ("мкм",), None, None),
            ("p_a", "Атмосферное давление", "101.325", ("кПа", "бар", "psi", "атм"), self.unit_pressure, self._on_pressure_unit_change),
            ("t_a", "Температура воздуха", "288.15", ("К", "°C"), self.unit_temp, self._on_temp_unit_change),
            ("n_m", "Измеренная мощность", "580.0", ("кВт", "л.с."), self.unit_power, self._on_power_unit_change),
            ("n_nom", "Номинальная (привед.)", "597.0", ("кВт", "л.с."), self.unit_power, self._on_power_unit_change),
        ]
        for key, label, default, units, unit_var, callback in fields:
            self._sidebar_field(sidebar, key, label, default, units, unit_var, callback)

        button_wrap = tk.Frame(sidebar, bg=C.BG_SIDEBAR)
        button_wrap.pack(fill=tk.X, padx=28, pady=(20, 12))
        analyze_btn = self._btn(button_wrap, "ВЫПОЛНИТЬ АНАЛИЗ", self.analyze, kind="secondary")
        analyze_btn.configure(font=("Segoe UI", 10, "bold"), pady=13)
        analyze_btn.pack(fill=tk.X)

        info = tk.Frame(sidebar, bg=C.BG_SIDEBAR)
        info.pack(side=tk.BOTTOM, fill=tk.X, padx=28, pady=18)
        tk.Label(info, text="ПОРОГИ ДИАГНОСТИКИ", font=("Segoe UI", 8, "bold"), bg=C.BG_SIDEBAR, fg=C.TEXT_SB_DIM).pack(anchor=tk.W)
        self.zeta_threshold_lbl: tk.Label | None = None
        for text, color in [
            (f"Эталон Ra = {RA_REFERENCE:g} мкм", C.SUCCESS),
            (f"Предупреждение > {RA_LIMIT_WARN:g} мкм", C.WARNING),
            (f"Критический > {RA_LIMIT_CRIT:g} мкм", C.DANGER),
            (f"Предел ζ = ζ_эт × {ZETA_LIMIT_FACTOR:g}", C.INFO),
        ]:
            row = tk.Frame(info, bg=C.BG_SIDEBAR)
            row.pack(anchor=tk.W, pady=2)
            tk.Label(row, text="●", fg=color, bg=C.BG_SIDEBAR, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            lbl = tk.Label(row, text=" " + text, fg=C.TEXT_SB, bg=C.BG_SIDEBAR, font=("Segoe UI", 9))
            lbl.pack(side=tk.LEFT)
            if text.startswith("Предел ζ"):
                self.zeta_threshold_lbl = lbl

    def _sidebar_field(
        self,
        parent: tk.Widget,
        key: str,
        label: str,
        default: str,
        unit_opts: tuple[str, ...],
        unit_var: tk.StringVar | None,
        unit_cb: Callable[[], None] | None,
    ) -> None:
        wrap = tk.Frame(parent, bg=C.BG_SIDEBAR)
        wrap.pack(fill=tk.X, padx=28, pady=5)
        tk.Label(wrap, text=label, font=("Segoe UI", 9), bg=C.BG_SIDEBAR, fg=C.TEXT_SB).pack(anchor=tk.W, pady=(0, 3))

        row = tk.Frame(wrap, bg=C.BG_SIDEBAR_2, highlightbackground="#1e3560", highlightthickness=1)
        row.pack(fill=tk.X)

        entry = tk.Entry(
            row,
            bg=C.BG_SIDEBAR_2,
            fg=C.TEXT_WHITE,
            insertbackground=C.TEXT_WHITE,
            relief="flat",
            bd=0,
            font=("Consolas", 11),
            highlightthickness=0,
        )
        entry.insert(0, default)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=8)
        self.entries[key] = entry

        if len(unit_opts) > 1 and unit_var is not None and unit_cb is not None:
            menu = tk.OptionMenu(row, unit_var, *unit_opts, command=lambda _: unit_cb())
            menu.configure(
                bg=C.UNIT_BTN,
                fg=C.UNIT_FG,
                activebackground=C.UNIT_BTN_HOV,
                activeforeground=C.TEXT_WHITE,
                relief="flat",
                bd=0,
                cursor="hand2",
                font=("Segoe UI", 8, "bold"),
                highlightthickness=0,
                padx=6,
                pady=3,
                indicatoron=False,
            )
            menu["menu"].configure(bg=C.BG_SIDEBAR_2, fg=C.TEXT_SB, activebackground=C.PRIMARY_LT, activeforeground=C.TEXT_WHITE)
            menu.pack(side=tk.RIGHT, padx=(0, 6))
        else:
            tk.Label(row, text=unit_opts[0], font=("Segoe UI", 9), bg=C.BG_SIDEBAR_2, fg=C.TEXT_SB_DIM).pack(side=tk.RIGHT, padx=10)

    def _build_main(self, parent: tk.Widget) -> None:
        main = tk.Frame(parent, bg=C.BG_APP)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_status_cards(main)

        notebook_wrap = tk.Frame(main, bg=C.BG_APP)
        notebook_wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=(6, 20))
        self.nb = ttk.Notebook(notebook_wrap, style="Modern.TNotebook")
        self.nb.pack(fill=tk.BOTH, expand=True)

        self.tab_trends = Card(self.nb)
        self.nb.add(self.tab_trends, text="  Тренды деградации  ")
        self._placeholder(self.tab_trends, "Графики деградации появятся после загрузки CSV и анализа.")

        self.tab_di = Card(self.nb)
        self.nb.add(self.tab_di, text="  Диагностический индекс  ")
        self._placeholder(self.tab_di, "Диагностический индекс DI появится после анализа.")

        self.tab_data = Card(self.nb)
        self.nb.add(self.tab_data, text="  Данные ANSYS  ")
        self._build_tab_data(self.tab_data)

        self.tab_report = Card(self.nb)
        self.nb.add(self.tab_report, text="  Отчёт  ")
        self._build_tab_report(self.tab_report)

        self.tab_econ = Card(self.nb)
        self.nb.add(self.tab_econ, text="  Экономика  ")
        self._build_tab_econ(self.tab_econ)

        self.tab_info = Card(self.nb)
        self.nb.add(self.tab_info, text="  Информация  ")
        self._build_tab_info(self.tab_info)

    def _build_status_cards(self, parent: tk.Widget) -> None:
        row = tk.Frame(parent, bg=C.BG_APP)
        row.pack(fill=tk.X, padx=20, pady=(20, 8))
        self.card_status = self._kpi_card(row, "СТАТУС", "-", "ожидание анализа", C.NEUTRAL)
        self.card_ra = self._kpi_card(row, "ШЕРОХОВАТОСТЬ Ra", "-", "мкм", C.INFO)
        self.card_power = self._kpi_card(row, "DELTA МОЩНОСТИ", "-", "+ дефицит / - избыток", C.INFO)
        self.card_di = self._kpi_card(row, "ИНДЕКС DI", "-", "0-100%", C.INFO)

    def _kpi_card(self, parent: tk.Widget, label: str, value: str, sub: str, accent: str) -> dict[str, tk.Widget]:
        card = Card(parent)
        card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6)
        strip = tk.Frame(card, bg=accent, height=4)
        strip.pack(fill=tk.X, side=tk.TOP)
        inner = tk.Frame(card, bg=C.BG_CARD)
        inner.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
        tk.Label(inner, text=label, font=("Segoe UI", 8, "bold"), bg=C.BG_CARD, fg=C.TEXT_MUTED).pack(anchor=tk.W)
        value_lbl = tk.Label(inner, text=value, font=("Segoe UI", 20, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK)
        value_lbl.pack(anchor=tk.W, pady=(4, 0))
        sub_lbl = tk.Label(inner, text=sub, font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT_MUTED)
        sub_lbl.pack(anchor=tk.W)
        return {"strip": strip, "value": value_lbl, "sub": sub_lbl}

    @staticmethod
    def _set_kpi(card: dict[str, tk.Widget], value: str, sub: str, accent: str) -> None:
        card["strip"].configure(bg=accent)
        card["value"].configure(text=value)
        card["sub"].configure(text=sub)

    def _placeholder(self, parent: tk.Widget, text: str) -> None:
        for child in parent.winfo_children():
            child.destroy()
        wrap = tk.Frame(parent, bg=C.BG_CARD)
        wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        tk.Label(wrap, text=text, font=("Segoe UI", 11), bg=C.BG_CARD, fg=C.TEXT_MUTED, justify=tk.CENTER).pack(expand=True)

    def _build_tab_data(self, parent: tk.Widget) -> None:
        wrap = tk.Frame(parent, bg=C.BG_CARD)
        wrap.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        head = tk.Frame(wrap, bg=C.BG_CARD)
        head.pack(fill=tk.X, pady=(0, 10))
        tk.Label(head, text="Результаты CFD-моделирования (ANSYS)", font=("Segoe UI", 12, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(
            side=tk.LEFT
        )
        self.data_count_lbl = tk.Label(head, text="", font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT_MUTED)
        self.data_count_lbl.pack(side=tk.LEFT, padx=12)

        table_wrap = tk.Frame(wrap, bg=C.BORDER, bd=0)
        table_wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table_wrap, show="headings", style="Modern.Treeview")
        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview, style="Modern.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview, style="Modern.Horizontal.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.tree.tag_configure("odd", background=C.BG_INPUT)
        self.tree.tag_configure("even", background=C.BG_CARD)

    def _build_tab_report(self, parent: tk.Widget) -> None:
        wrap = tk.Frame(parent, bg=C.BG_CARD)
        wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        tk.Label(wrap, text="Диагностический отчёт", font=("Segoe UI", 13, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(anchor=tk.W)
        tk.Label(wrap, text="Детализация результатов анализа и рекомендаций", font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT_MUTED).pack(
            anchor=tk.W, pady=(0, 12)
        )

        text_frame = tk.Frame(wrap, bg=C.BORDER_DK, bd=0, highlightbackground=C.BORDER, highlightthickness=1)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.res_txt = tk.Text(
            text_frame,
            font=("Consolas", 10),
            bg="#0d1b3e",
            fg="#dde8f8",
            insertbackground=C.TEXT_WHITE,
            relief="flat",
            bd=0,
            padx=16,
            pady=12,
            wrap=tk.NONE,
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.res_txt.yview, style="Modern.Vertical.TScrollbar")
        self.res_txt.configure(yscrollcommand=scrollbar.set)
        self.res_txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for tag, color in {"success": "#34d399", "warn": "#fbbf24", "danger": "#f87171", "info": "#60a5fa", "head": "#93c5fd"}.items():
            self.res_txt.tag_configure(tag, foreground=color, font=("Consolas", 10, "bold") if tag in {"success", "warn", "danger", "head"} else None)
        self._write_report("Отчёт появится здесь после выполнения анализа.")

    def _build_tab_econ(self, parent: tk.Widget) -> None:
        pane = tk.Frame(parent, bg=C.BG_CARD)
        pane.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        tk.Label(pane, text="Экономический анализ технического состояния", font=("Segoe UI", 13, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(
            anchor=tk.W
        )
        tk.Label(
            pane,
            text="Оценка прироста расхода топлива и сравнение сценариев технического обслуживания.",
            font=("Segoe UI", 9),
            bg=C.BG_CARD,
            fg=C.TEXT_MUTED,
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 14))

        top = tk.Frame(pane, bg=C.BG_CARD)
        top.pack(fill=tk.X)
        input_card = Card(top)
        input_card.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        input_inner = tk.Frame(input_card, bg=C.BG_CARD)
        input_inner.pack(fill=tk.BOTH, padx=16, pady=14)
        tk.Label(input_inner, text="ИСХОДНЫЕ ДАННЫЕ", font=("Segoe UI", 8, "bold"), bg=C.BG_CARD, fg=C.TEXT_MUTED).pack(
            anchor=tk.W, pady=(0, 8)
        )

        fields = [
            ("fuel_price", "Цена топлива, руб/кг", "70"),
            ("fuel_flow_ref", "Базовый расход топлива, кг/ч", "130"),
            ("annual_hours", "Налёт в год, ч", "800"),
            ("cost_mro", "Стоимость полного ремонта (MRO), руб", "3000000"),
            ("cost_wash", "Стоимость промывки ПЧ, руб", "120000"),
            ("discount_rate", "Ставка дисконтирования, %", "10"),
            ("horizon", "Горизонт планирования, лет", "5"),
        ]
        for key, label, default in fields:
            row = tk.Frame(input_inner, bg=C.BG_CARD)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT, width=38, anchor=tk.W).pack(side=tk.LEFT)
            entry = tk.Entry(
                row,
                font=("Consolas", 10),
                width=14,
                bg=C.BG_INPUT,
                fg=C.TEXT_DARK,
                relief="flat",
                highlightthickness=1,
                highlightbackground=C.BORDER,
                highlightcolor=C.PRIMARY,
            )
            entry.insert(0, default)
            entry.pack(side=tk.LEFT, padx=(8, 0))
            self.econ_entries[key] = entry

        self._btn(input_inner, "Рассчитать экономику", self.render_economics, kind="secondary").pack(fill=tk.X, pady=(14, 0))

        result_card = Card(top)
        result_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        result_inner = tk.Frame(result_card, bg=C.BG_CARD)
        result_inner.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)
        tk.Label(result_inner, text="РЕЗУЛЬТАТЫ", font=("Segoe UI", 8, "bold"), bg=C.BG_CARD, fg=C.TEXT_MUTED).pack(anchor=tk.W, pady=(0, 4))
        self.econ_result_frame = tk.Frame(result_inner, bg=C.BG_CARD)
        self.econ_result_frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            self.econ_result_frame,
            text="Выполните анализ диагностических параметров, затем нажмите «Рассчитать экономику».",
            font=("Segoe UI", 9),
            bg=C.BG_CARD,
            fg=C.TEXT_MUTED,
            wraplength=500,
        ).pack(pady=30)

        self.econ_chart_frame = tk.Frame(pane, bg=C.BG_CARD)
        self.econ_chart_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

    def _build_tab_info(self, parent: tk.Widget) -> None:
        outer = tk.Frame(parent, bg=C.BG_CARD)
        outer.pack(fill=tk.BOTH, expand=True, padx=28, pady=24)
        tk.Label(outer, text="Информация", font=("Segoe UI", 13, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(anchor=tk.W)
        tk.Label(
            outer,
            text=(
                "Эталонные CFD-параметры рассчитываются интерполяцией при Ra = "
                f"{RA_REFERENCE:g} мкм. Если CSV не покрывает эту точку, используется минимальная Ra таблицы."
            ),
            font=("Segoe UI", 9),
            bg=C.BG_CARD,
            fg=C.TEXT_MUTED,
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 18))

        for title, body in [
            (
                "Знаковое соглашение Delta N",
                "Delta N = (N_ожид - N_привед) / N_ожид * 100 %\n"
                "Delta N > 0: дефицит мощности\n"
                "Delta N < 0: фактическая мощность выше модели",
            ),
            (
                "Пороговые значения",
                f"Эталон Ra: {RA_REFERENCE:g} мкм\n"
                f"Предупреждение: Ra > {RA_LIMIT_WARN:g} мкм\n"
                f"Критический уровень: Ra > {RA_LIMIT_CRIT:g} мкм\n"
                f"Предел zeta: эталонное ζ (при ref Ra) × {ZETA_LIMIT_FACTOR:g}",
            ),
        ]:
            card = Card(outer)
            card.pack(fill=tk.X, pady=6)
            inner = tk.Frame(card, bg=C.BG_CARD)
            inner.pack(fill=tk.X, padx=18, pady=14)
            tk.Label(inner, text=title, font=("Segoe UI", 10, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(anchor=tk.W)
            tk.Label(inner, text=body, font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        # Карточка автора
        au_card = Card(outer)
        au_card.pack(fill=tk.X, pady=6)
        au_inner = tk.Frame(au_card, bg=C.BG_CARD)
        au_inner.pack(fill=tk.X, padx=18, pady=14)
        tk.Label(au_inner, text="Об авторе", font=("Segoe UI", 10, "bold"),
                 bg=C.BG_CARD, fg=C.TEXT_DARK).pack(anchor=tk.W)
        tk.Label(au_inner, text="Мальцев Дмитрий Сергеевич",
                 font=("Segoe UI", 11, "bold"),
                 bg=C.BG_CARD, fg=C.PRIMARY).pack(anchor=tk.W, pady=(6, 0))
        tk.Label(au_inner,
                 text="Московский государственный технический университет гражданской авиации (МГТУ ГА)",
                 font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT,
                 wraplength=700, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        tk.Label(au_inner,
                 text="Программа выполнена в приложении к дипломной работе.",
                 font=("Segoe UI", 9, "italic"),
                 bg=C.BG_CARD, fg=C.TEXT_MUTED).pack(anchor=tk.W, pady=(4, 0))

    def _on_pressure_unit_change(self) -> None:
        new_unit = self.unit_pressure.get()
        old_unit = self.prev_pressure_unit
        if new_unit == old_unit:
            return
        try:
            value = from_kpa(to_kpa(parse_float(self.entries["p_a"].get(), "Атмосферное давление"), old_unit), new_unit)
            self.entries["p_a"].delete(0, tk.END)
            self.entries["p_a"].insert(0, fmt_num(value))
        except ValueError:
            pass
        self.prev_pressure_unit = new_unit

    def _on_temp_unit_change(self) -> None:
        new_unit = self.unit_temp.get()
        old_unit = self.prev_temp_unit
        if new_unit == old_unit:
            return
        try:
            value = from_k(to_k(parse_float(self.entries["t_a"].get(), "Температура воздуха"), old_unit), new_unit)
            self.entries["t_a"].delete(0, tk.END)
            self.entries["t_a"].insert(0, fmt_num(value))
        except ValueError:
            pass
        self.prev_temp_unit = new_unit

    def _on_power_unit_change(self) -> None:
        new_unit = self.unit_power.get()
        old_unit = self.prev_power_unit
        if new_unit == old_unit:
            return
        for key, name in (("n_m", "Измеренная мощность"), ("n_nom", "Номинальная мощность")):
            try:
                value = from_kw(to_kw(parse_float(self.entries[key].get(), name), old_unit), new_unit)
                self.entries[key].delete(0, tk.END)
                self.entries[key].insert(0, fmt_num(value))
            except ValueError:
                pass
        self.prev_power_unit = new_unit

    def load_csv(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            points = read_ansys_csv(path)
            self.model = CfdModel(points)
            self.last = None
            self.csv_path = path
            self._update_table()
            self._update_csv_status()
            msg = (
                f"Загружено точек: {len(self.model.points)}\n"
                f"Диапазон Ra: {self.model.ra_min:.1f}–{self.model.ra_max:.1f} мкм\n"
                f"Эталон расчёта: Ra = {self.model.ref_ra:.1f} мкм"
            )
            if self.model.ref_ra != RA_REFERENCE:
                msg += f"\n\nВнимание: Ra={RA_REFERENCE:g} мкм отсутствует в диапазоне CSV."
            messagebox.showinfo("Импорт завершён", msg)
        except Exception as exc:
            messagebox.showerror("Ошибка импорта", str(exc))

    def _update_csv_status(self) -> None:
        if self.model is None:
            return
        self.csv_status_lbl.configure(text=f"● Загружено: {len(self.model.points)} точек", fg="#34d399")
        self.csv_meta_lbl.configure(
            text=f"Ra: {self.model.ra_min:.1f}–{self.model.ra_max:.1f} мкм; эталон: {self.model.ref_ra:.1f} мкм"
        )
        self.data_count_lbl.configure(text=f"— всего {len(self.model.points)} точек")
        if self.zeta_threshold_lbl is not None and self.model.zeta_limit is not None:
            self.zeta_threshold_lbl.configure(
                text=f" Предел ζ = {self.model.zeta_limit:.4f} (эт.×{ZETA_LIMIT_FACTOR:g})"
            )

    def _update_table(self) -> None:
        if self.model is None:
            return
        for item in self.tree.get_children():
            self.tree.delete(item)

        names = [str(point.get("Name", f"DP {idx}")) for idx, point in enumerate(self.model.points, 1)]
        columns = ["Параметр", "Ед."] + names
        self.tree["columns"] = columns
        for col in columns:
            self.tree.heading(col, text=col)
            if col == "Параметр":
                self.tree.column(col, width=220, anchor=tk.W, minwidth=160)
            elif col == "Ед.":
                self.tree.column(col, width=55, anchor=tk.CENTER, minwidth=40)
            else:
                self.tree.column(col, width=105, anchor=tk.CENTER, minwidth=80)

        for idx, (code, (name, unit, decimals)) in enumerate(PARAM_MAP.items()):
            row = [name, unit]
            for point in self.model.points:
                try:
                    value = float(point[code])
                    if code in ("P4", "P5", "P6"):
                        value /= 1000.0
                    row.append(f"{value:.{decimals}f}")
                except (KeyError, ValueError, TypeError):
                    row.append("-")
            self.tree.insert("", tk.END, values=row, tags=("even" if idx % 2 == 0 else "odd",))

    def _read_stand_inputs(self) -> StandInputs:
        ra = parse_float(self.entries["ra_meas"].get(), "Ra после ремонта")
        pressure_raw = parse_float(self.entries["p_a"].get(), "Атмосферное давление")
        temp_raw = parse_float(self.entries["t_a"].get(), "Температура воздуха")
        measured_raw = parse_float(self.entries["n_m"].get(), "Измеренная мощность")
        nominal_raw = parse_float(self.entries["n_nom"].get(), "Номинальная мощность")

        pressure_unit = self.unit_pressure.get()
        temp_unit = self.unit_temp.get()
        power_unit = self.unit_power.get()
        return StandInputs(
            ra=ra,
            pressure_kpa=to_kpa(pressure_raw, pressure_unit),
            temp_k=to_k(temp_raw, temp_unit),
            measured_kw=to_kw(measured_raw, power_unit),
            nominal_kw=to_kw(nominal_raw, power_unit),
            pressure_display=pressure_raw,
            temp_display=temp_raw,
            measured_display=measured_raw,
            nominal_display=nominal_raw,
            pressure_unit=pressure_unit,
            temp_unit=temp_unit,
            power_unit=power_unit,
        )

    def analyze(self) -> None:
        if self.model is None:
            messagebox.showwarning("Нет данных", "Сначала загрузите CSV-файл ANSYS.")
            return
        try:
            inputs = self._read_stand_inputs()
            self.last = self.model.analyze(inputs)
        except ValueError as exc:
            messagebox.showerror("Ошибка ввода", str(exc))
            return

        result = self.last
        self._set_kpi(self.card_status, result.status, result.status_sub, result.accent)
        self._set_kpi(
            self.card_ra,
            f"{result.ra:.1f}",
            "мкм",
            C.DANGER if result.ra > RA_LIMIT_CRIT else C.WARNING if result.ra > RA_LIMIT_WARN else C.SUCCESS,
        )
        self._set_kpi(
            self.card_power,
            f"{result.power_dev:+.2f}%",
            "+ дефицит / - избыток",
            C.DANGER if result.power_dev > 10 else C.WARNING if result.power_dev > 5 else C.SUCCESS if abs(result.power_dev) < 2 else C.INFO,
        )
        self._set_kpi(self.card_di, f"{result.di:.1f}%", "диагностический индекс", C.DANGER if result.di >= 70 else C.WARNING if result.di >= 30 else C.SUCCESS)

        self.render_trends()
        self.render_di()
        self.render_report()
        self.render_economics(silent=True)

    def render_trends(self) -> None:
        if self.model is None:
            return
        if self.fig_trends is not None:
            plt.close(self.fig_trends)
            self.fig_trends = None
        for child in self.tab_trends.winfo_children():
            child.destroy()

        ra = self.model.ra
        ra_fine = np.linspace(self.model.ra_min, self.model.ra_max, 300)
        ra_current = self.last.ra if self.last else None

        fig = Figure(figsize=(12, 7), dpi=95, facecolor=C.BG_CARD)
        fig.suptitle("Деградация параметров лопатки СА в зависимости от Ra", fontsize=12, fontweight="bold", color=C.TEXT_DARK, y=0.98)
        self.fig_trends = fig
        axes = fig.subplots(2, 2)
        fig.subplots_adjust(hspace=0.42, wspace=0.28, left=0.07, right=0.97, top=0.90, bottom=0.08)

        config = [
            (axes[0, 0], "P1", "Массовый расход G, кг/с", C.CHART_2, False),
            (axes[0, 1], "P4", "Полное давление вых., кПа", C.PRIMARY_LT, True),
            (axes[1, 0], "P7", "Коэффициент потерь zeta", C.DANGER, False),
            (axes[1, 1], "P8", "Число Маха (выход)", "#7c3aed", False),
        ]

        for ax, code, title, color, kpa in config:
            if code not in self.model.interp:
                ax.set_title(f"{title}: нет данных")
                ax.grid(True, alpha=0.4)
                continue
            raw = np.array([float(point[code]) for point in self.model.points])
            fine = np.array(self.model.interp[code](ra_fine))
            if kpa:
                raw /= 1000.0
                fine /= 1000.0
            ax.plot(ra_fine, fine, color=color, linewidth=2.2, alpha=0.9, label="Интерполяция")
            ax.scatter(ra, raw, color=color, s=42, zorder=5, edgecolors="white", linewidths=1.3, label="ANSYS CFD")
            ax.axvline(self.model.ref_ra, color=C.SUCCESS, linewidth=1.0, linestyle=":", alpha=0.9, label=f"Эталон Ra={self.model.ref_ra:g}")
            if code == "P7" and self.model.zeta_limit is not None:
                ax.axhline(self.model.zeta_limit, color=C.DANGER, linewidth=1.2, linestyle="--", alpha=0.8, label=f"ζ-предел = {self.model.zeta_limit:.4f}")
            ax.axvspan(RA_LIMIT_WARN, RA_LIMIT_CRIT, color=C.WARNING, alpha=0.07)
            ax.axvspan(RA_LIMIT_CRIT, max(ra.max() * 1.1, RA_LIMIT_CRIT + 1), color=C.DANGER, alpha=0.07)

            if ra_current is not None:
                current = self.model.value(code, ra_current)
                if current is not None:
                    if kpa:
                        current /= 1000.0
                    ax.scatter([ra_current], [current], s=160, color="none", edgecolors=C.TEXT_DARK, linewidths=2.0, zorder=6, label=f"Текущее Ra={ra_current:.1f}")
            ax.set_title(title, pad=8)
            ax.set_xlabel("Ra, мкм")
            ax.legend(loc="best")
            ax.grid(True, alpha=0.4)

        chart_wrap = tk.Frame(self.tab_trends, bg=C.BG_CARD)
        chart_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        canvas = FigureCanvasTkAgg(fig, master=chart_wrap)
        canvas.draw()
        toolbar = NavigationToolbar2Tk(canvas, chart_wrap)
        toolbar.update()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.nb.select(self.tab_trends)

    def render_di(self) -> None:
        if self.model is None or self.last is None:
            return
        if self.fig_di is not None:
            plt.close(self.fig_di)
            self.fig_di = None
        for child in self.tab_di.winfo_children():
            child.destroy()

        result = self.last
        ra_fine = np.linspace(self.model.ra_min, self.model.ra_max, 300)
        di_curve = np.array([self.model.calc_di(float(ra), 0.0) for ra in ra_fine])

        fig = Figure(figsize=(13, 5.8), dpi=95, facecolor=C.BG_CARD)
        fig.suptitle("Диагностический индекс DI — лопатка СА GE H80-200", fontsize=12, fontweight="bold", color=C.TEXT_DARK)
        self.fig_di = fig
        fig.subplots_adjust(wspace=0.35, left=0.07, right=0.96, top=0.88, bottom=0.11)

        ax1 = fig.add_subplot(121)
        ax1.axhspan(0, 30, color=C.SUCCESS, alpha=0.10)
        ax1.axhspan(30, 70, color=C.WARNING, alpha=0.10)
        ax1.axhspan(70, 100, color=C.DANGER, alpha=0.10)
        ax1.plot(ra_fine, di_curve, color=C.TEXT_DARK, linewidth=2, linestyle="--", label="DI (ΔN=0, ANSYS CFD)")
        point_color = C.SUCCESS if result.di < 30 else C.WARNING if result.di < 70 else C.DANGER
        ax1.scatter([result.ra], [result.di], color=point_color, s=140, zorder=6, edgecolors=C.TEXT_DARK, linewidths=1.2, label=f"Текущее: DI={result.di:.1f}%")
        ax1.axhline(30, color=C.WARNING, lw=1, alpha=0.6)
        ax1.axhline(70, color=C.DANGER, lw=1, alpha=0.6)
        ax1.axvline(result.ra, color=point_color, lw=0.8, ls=":", alpha=0.5)
        ax1.set_xlabel("Ra, мкм")
        ax1.set_ylabel("Диагностический индекс DI, %")
        ax1.set_ylim(0, 105)
        ax1.set_xlim(self.model.ra_min - 2, self.model.ra_max + 2)
        ax1.set_title("DI = f(Ra, N_факт)", pad=8)
        ax1.grid(True, alpha=0.4)

        patches = [
            mpatches.Patch(color=C.SUCCESS, alpha=0.45, label="Норма (< 30%)"),
            mpatches.Patch(color=C.WARNING, alpha=0.45, label="Контроль (30-70%)"),
            mpatches.Patch(color=C.DANGER, alpha=0.45, label="Ремонт (> 70%)"),
        ]
        handles, _ = ax1.get_legend_handles_labels()
        ax1.legend(handles=handles + patches, loc="upper left", fontsize=7.5)

        codes = ["P7", "P1", "P8", "P9", "__PWR__"]
        labels = ["zeta\nпотери", "Расход\nG", "Число\nМаха", "Угол\nalpha", "Delta\nмощн."]
        values = []
        for code in codes:
            if code == "__PWR__":
                value = min(max(result.power_dev, 0.0) / 10.0, 1.0) * 100.0
            elif code in self.model.interp and code in self.model.param_ranges:
                ref = self.model.ref_params.get(code, 0.0)
                cur_raw = self.model.value(code, result.ra)
                cur = cur_raw if cur_raw is not None else ref
                value = min(abs(cur - ref) / self.model.param_ranges[code], 1.0) * 100.0
            else:
                value = 0.0
            values.append(value)

        angles = np.linspace(0, 2 * np.pi, len(codes), endpoint=False).tolist()
        closed_values = values + [values[0]]
        closed_angles = angles + [angles[0]]
        ax2 = fig.add_subplot(122, polar=True)
        ax2.plot(closed_angles, closed_values, "o-", color=C.PRIMARY_LT, linewidth=2.2, markersize=7)
        ax2.fill(closed_angles, closed_values, alpha=0.22, color=C.PRIMARY_LT)
        ax2.set_xticks(angles)
        ax2.set_xticklabels(labels, fontsize=9, color=C.TEXT_DARK)
        ax2.set_ylim(0, 100)
        ax2.set_yticks([25, 50, 75, 100])
        ax2.set_yticklabels(["25%", "50%", "75%", "100%"], fontsize=7, color=C.TEXT_MUTED)
        ax2.spines["polar"].set_color(C.BORDER_DK)
        ax2.set_title(f"Вклад факторов в DI\nRa={result.ra:.1f} мкм • Delta N={result.power_dev:+.1f}% • DI={result.di:.1f}%", fontsize=9.5, pad=18)

        chart_wrap = tk.Frame(self.tab_di, bg=C.BG_CARD)
        chart_wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        canvas = FigureCanvasTkAgg(fig, master=chart_wrap)
        canvas.draw()
        toolbar = NavigationToolbar2Tk(canvas, chart_wrap)
        toolbar.update()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def render_report(self) -> None:
        if self.model is None or self.last is None:
            return
        result = self.last
        ts = datetime.now().strftime("%d.%m.%Y %H:%M")
        power_unit = result.power_unit
        measured = result.measured_display
        nominal = result.nominal_display
        reduced = from_kw(result.reduced_kw, power_unit)
        expected = from_kw(result.expected_kw, power_unit)

        interp_lines = []
        for code in ("P1", "P4", "P7", "P8", "P9"):
            value = self.model.value(code, result.ra)
            if value is None:
                continue
            name, unit, decimals = PARAM_MAP[code]
            if code in ("P4", "P5", "P6"):
                value /= 1000.0
            interp_lines.append(f"    {name:<28}: {value:.{decimals}f} {unit}")

        lines = [
            "=" * 64,
            f"  ДИАГНОСТИЧЕСКИЙ ОТЧЁТ   {ts}",
            "  Двигатель GE H80-200 | Узел: Лопатка СА | МГТУ ГА",
            "=" * 64,
            "",
            "  СТЕНДОВЫЕ ДАННЫЕ:",
            f"    Ra (измерено)         : {result.ra:.1f} мкм",
            f"    N_изм                 : {measured:.2f} {power_unit}",
            f"    P_атм                 : {result.pressure_display:.4g} {result.pressure_unit}",
            f"    T_воздуха             : {result.temp_display:.4g} {result.temp_unit}",
            f"    N_ном (привед.)       : {nominal:.2f} {power_unit}",
            "",
            "  ПРИВЕДЕНИЕ К СТАНДАРТНЫМ УСЛОВИЯМ:",
            f"    delta = p_a/101.325   : {result.delta:.4f}",
            f"    sqrt(theta)           : {math.sqrt(result.theta):.4f}",
            f"    N_прив (факт)         : {reduced:.2f} {power_unit}",
            "",
            f"  МОДЕЛЬ ANSYS (Ra={result.ra:.1f} мкм; эталон Ra={result.ref_ra:.1f} мкм):",
            f"    G(Ra)/G_ref           : {result.flow_ratio:.5f}",
            f"    N_ожид по модели      : {expected:.2f} {power_unit}",
            *interp_lines,
            "",
            "  СИГНАЛ A: МОЩНОСТЬ",
            f"    Delta N = (N_ожид - N_привед) / N_ожид * 100% : {result.power_dev:+.2f}%",
            "    Знак: + = дефицит; - = избыток относительно модели",
        ]
        if abs(result.power_dev) <= 2.0:
            lines.append("    Интерпретация         : соответствует модели")
        elif result.power_dev > 0:
            lines.append(f"    Интерпретация         : дефицит мощности ({'значительный' if result.power_dev > 5 else 'умеренный'})")
        else:
            lines.append("    Интерпретация         : мощность выше модели; проверьте N_ном и стендовые данные")

        lines.extend(
            [
                "",
                "  СИГНАЛ B: АЭРОДИНАМИКА ЛОПАТКИ",
                f"    Delta zeta            : {result.zeta_deg:+.2f}%",
                f"    zeta-предел (эт.×{ZETA_LIMIT_FACTOR:g}) : {result.zeta_ref * ZETA_LIMIT_FACTOR:.4f}",
                f"    Delta расхода         : {result.flow_loss:+.3f}%",
                f"    DI                    : {result.di:.1f}%",
            ]
        )
        if result.ra_threshold is not None:
            lines.append(f"    Прогноз zeta-порога   : Ra около {result.ra_threshold:.0f} мкм")

        lines.extend(["", "-" * 64, f"  ИТОГОВЫЙ СТАТУС: {result.status}", "-" * 64, "", self._recommendation(result), "", "=" * 64])
        self._write_report("\n".join(lines), result.status_tag)

    @staticmethod
    def _recommendation(result: AnalysisResult) -> str:
        if result.power_dev > 10.0 and result.ra > RA_LIMIT_CRIT:
            return (
                "  Критическое состояние: значительный дефицит мощности при высокой шероховатости.\n"
                "  Рекомендуется останов двигателя, полный осмотр и ремонт лопаточного аппарата СА."
            )
        if result.power_dev > 10.0:
            return (
                "  Значительный дефицит мощности при допустимой Ra.\n"
                "  Вероятны отклонения в других узлах двигателя; нужна расширенная диагностика."
            )
        if result.ra > RA_LIMIT_CRIT:
            return "  Шероховатость критическая. Требуется промывка ПЧ и повторный контроль Ra."
        if result.power_dev > 5.0 or result.ra > RA_LIMIT_WARN or result.di >= 30.0:
            return "  Умеренная деградация. Рекомендуется плановая промывка и контроль на следующем цикле."
        if result.power_dev < -5.0:
            return "  Мощность выше модели. Проверьте корректность N_ном, условий приведения и стендовых измерений."
        return "  Двигатель в норме. Параметры соответствуют модели ANSYS."

    def _write_report(self, text: str, tag: str | None = None) -> None:
        self.res_txt.config(state=tk.NORMAL)
        self.res_txt.delete("1.0", tk.END)
        self.res_txt.insert(tk.END, text)
        self._highlight_report(tag)
        self.res_txt.config(state=tk.DISABLED)

    def _highlight_report(self, tag: str | None) -> None:
        for key in ("СТЕНДОВЫЕ ДАННЫЕ:", "МОДЕЛЬ ANSYS", "ПРИВЕДЕНИЕ", "СИГНАЛ A", "СИГНАЛ B"):
            idx = "1.0"
            while True:
                pos = self.res_txt.search(key, idx, stopindex=tk.END)
                if not pos:
                    break
                end = self.res_txt.index(f"{pos} lineend")
                self.res_txt.tag_add("head", f"{pos} linestart", end)
                idx = end
        pos = self.res_txt.search("ИТОГОВЫЙ СТАТУС:", "1.0", stopindex=tk.END)
        if pos:
            self.res_txt.tag_add(tag or "info", f"{pos} linestart", f"{pos} lineend")

    def render_economics(self, *, silent: bool = False) -> None:
        if self.last is None:
            if not silent:
                messagebox.showwarning("Нет анализа", "Сначала выполните диагностический анализ.")
            return

        try:
            fuel_price = parse_float(self.econ_entries["fuel_price"].get(), "Цена топлива")
            flow_ref = parse_float(self.econ_entries["fuel_flow_ref"].get(), "Базовый расход топлива")
            annual_hours = parse_float(self.econ_entries["annual_hours"].get(), "Налёт в год")
            cost_mro = parse_float(self.econ_entries["cost_mro"].get(), "Стоимость MRO")
            cost_wash = parse_float(self.econ_entries["cost_wash"].get(), "Стоимость промывки")
            rate = parse_float(self.econ_entries["discount_rate"].get(), "Ставка дисконтирования") / 100.0
            horizon = int(parse_float(self.econ_entries["horizon"].get(), "Горизонт планирования"))
            if fuel_price < 0 or flow_ref <= 0 or annual_hours <= 0 or cost_mro < 0 or cost_wash < 0 or rate <= -1 or horizon < 1:
                raise ValueError("Экономические параметры должны быть положительными; ставка должна быть больше -100%.")
        except ValueError as exc:
            if not silent:
                messagebox.showerror("Ошибка экономики", str(exc))
            return

        result = self.last
        degradation_factor = (
            max(result.power_dev, 0.0) / 100.0
            + 0.3 * max(result.zeta_deg, 0.0) / 100.0
            + 0.5 * max(result.flow_loss, 0.0) / 100.0
        )
        extra_fuel_h = flow_ref * degradation_factor
        extra_fuel_year = extra_fuel_h * annual_hours
        extra_cost_year = extra_fuel_year * fuel_price
        wash_eff = 0.55
        # 1 промывка сейчас (учтена как инвестиция cost_wash в год 0) + 1 промывка в год далее
        wash_cycles_year = 1.0
        wash_annual_cost = wash_cycles_year * cost_wash + extra_cost_year * (1.0 - wash_eff)

        years = np.arange(0, horizon + 1)

        # Формула учебника: NPV = -K + Σ(CF_j / (1+i)^j), где CF_j — экономия в год j
        def npv_series(annual_savings: float, invest: float) -> np.ndarray:
            acc = -invest
            values = [-invest]
            for year in range(1, horizon + 1):
                acc += annual_savings / (1 + rate) ** year
                values.append(acc)
            return np.array(values)

        wash_savings_year = extra_cost_year - wash_annual_cost
        npv_status_quo = npv_series(0.0, 0.0)                        # базовая линия
        npv_wash = npv_series(wash_savings_year, cost_wash)
        npv_mro = npv_series(extra_cost_year, cost_mro)

        payback_mro = self._payback_year(extra_cost_year, cost_mro, rate, horizon)
        payback_wash = self._payback_year(extra_cost_year - wash_annual_cost, cost_wash, rate, horizon)

        for child in self.econ_result_frame.winfo_children():
            child.destroy()

        def row(parent: tk.Widget, label: str, value: str, color: str = C.TEXT) -> None:
            item = tk.Frame(parent, bg=C.BG_CARD)
            item.pack(fill=tk.X, pady=2)
            tk.Label(item, text=label, font=("Segoe UI", 9), bg=C.BG_CARD, fg=C.TEXT_MUTED, width=36, anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(item, text=value, font=("Segoe UI", 9, "bold"), bg=C.BG_CARD, fg=color).pack(side=tk.LEFT)

        deg_pct = degradation_factor * 100.0
        deg_color = C.DANGER if deg_pct > 5 else C.WARNING if deg_pct > 2 else C.SUCCESS
        tk.Label(self.econ_result_frame, text="Прирост расхода топлива", font=("Segoe UI", 10, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(
            anchor=tk.W, pady=(0, 4)
        )
        row(self.econ_result_frame, "Фактор деградации Delta SFC", f"{deg_pct:.2f} %", deg_color)
        row(self.econ_result_frame, "Доп. расход топлива", f"{extra_fuel_h:.2f} кг/ч ({extra_fuel_year:,.0f} кг/год)")
        row(self.econ_result_frame, "Потери на топливо в год", f"{extra_cost_year:,.0f} руб/год", deg_color)

        tk.Frame(self.econ_result_frame, bg=C.BORDER, height=1).pack(fill=tk.X, pady=8)
        tk.Label(self.econ_result_frame, text="Сценарии технического обслуживания", font=("Segoe UI", 10, "bold"), bg=C.BG_CARD, fg=C.TEXT_DARK).pack(
            anchor=tk.W, pady=(0, 4)
        )

        scenarios = [
            ("Status Quo", 0.0, 0.0, npv_status_quo[-1], "-"),
            ("Промывка ПЧ", cost_wash, wash_savings_year, npv_wash[-1], f"{payback_wash} лет" if payback_wash else f"> {horizon} лет"),
            ("Полный ремонт MRO", cost_mro, extra_cost_year, npv_mro[-1], f"{payback_mro} лет" if payback_mro else f"> {horizon} лет"),
        ]
        best = max(item[3] for item in scenarios)

        tbl = tk.Frame(self.econ_result_frame, bg=C.BG_CARD)
        tbl.pack(fill=tk.X, pady=(0, 4))
        col_headers = ["Сценарий", "Инвестиции, руб", "Экономия/год, руб", "NPV, руб", "Окупаемость"]
        for j, h in enumerate(col_headers):
            tk.Label(
                tbl, text=h, font=("Segoe UI", 8, "bold"),
                bg=C.BG_INPUT, fg=C.TEXT_MUTED, anchor=tk.W, padx=8, pady=4,
            ).grid(row=0, column=j, sticky="ew", padx=(0, 1), pady=(0, 2))
            tbl.columnconfigure(j, weight=1)
        for i, (name, invest, annual, npv_value, payback) in enumerate(scenarios, 1):
            is_best = abs(npv_value - best) < 1.0
            row_bg = C.SUCCESS_BG if is_best else (C.BG_INPUT if i % 2 == 1 else C.BG_CARD)
            fg = C.SUCCESS if is_best else C.TEXT_DARK
            font = ("Segoe UI", 9, "bold") if is_best else ("Segoe UI", 9)
            vals = [name, f"{invest:,.0f}", f"{annual:,.0f}", f"{npv_value:,.0f}", payback]
            for j, val in enumerate(vals):
                tk.Label(
                    tbl, text=val, font=font,
                    bg=row_bg, fg=fg, anchor=tk.W, padx=8, pady=5,
                ).grid(row=i, column=j, sticky="ew", padx=(0, 1), pady=1)

        if self.fig_econ is not None:
            plt.close(self.fig_econ)
            self.fig_econ = None
        for child in self.econ_chart_frame.winfo_children():
            child.destroy()

        fig = Figure(figsize=(12, 3.6), dpi=95, facecolor=C.BG_CARD)
        fig.suptitle(f"NPV = −K + Σ CF_j/(1+r)^j   —   чистая дисконтированная стоимость (r = {rate * 100:.0f}%)", fontsize=10, fontweight="bold", color=C.TEXT_DARK)
        self.fig_econ = fig
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=0.09, right=0.97, top=0.82, bottom=0.14)
        ax.plot(years, npv_status_quo / 1e6, color=C.DANGER, linewidth=2.2, marker="o", markersize=4, label="Status Quo")
        ax.plot(years, npv_wash / 1e6, color=C.WARNING, linewidth=2.2, marker="s", markersize=4, label="Промывка ПЧ")
        ax.plot(years, npv_mro / 1e6, color=C.SUCCESS, linewidth=2.2, marker="^", markersize=4, label="MRO")
        ax.axhline(0, color=C.BORDER_DK, linewidth=0.8, linestyle="--")
        ax.set_xlabel("Год")
        ax.set_ylabel("NPV, млн руб.")
        ax.legend(loc="lower left", fontsize=8)
        ax.grid(True, alpha=0.4)

        canvas = FigureCanvasTkAgg(fig, master=self.econ_chart_frame)
        canvas.draw()
        toolbar = NavigationToolbar2Tk(canvas, self.econ_chart_frame)
        toolbar.update()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

    @staticmethod
    def _payback_year(annual_savings: float, invest: float, rate: float, horizon: int) -> int | None:
        if annual_savings <= 0:
            return None
        savings = 0.0
        for year in range(1, horizon + 1):
            savings += annual_savings / (1 + rate) ** year
            if savings >= invest:
                return year
        return None

    def export_report(self) -> None:
        content = self.res_txt.get("1.0", tk.END).strip()
        if not content or self.last is None:
            messagebox.showwarning("Нет данных", "Сначала выполните анализ.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Текстовый отчёт", "*.txt")],
            initialfile=f"H80_diagnostic_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write(content)
            messagebox.showinfo("Экспорт", f"Отчёт сохранён:\n{path}")
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения", str(exc))


if __name__ == "__main__":
    root = tk.Tk()
    DiagnosticApp(root)
    root.mainloop()
