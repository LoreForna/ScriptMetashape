# -*- coding: utf-8 -*-
"""
normal_ortho_metric_A_B_manualgrid.py

Ortomosaico delle NORMALI orientato su assi definiti da 3 marker gia' presenti
nel progetto, scelti da menu a tendina. Pensato per analisi metrologiche del
paramento e per sovrapposizione pixel-perfect con un ortomosaico gia' esportato.

MODALITA'
---------
A) Automatica classica
   La griglia raster viene calcolata dalla mesh clippata sul bounding box del chunk
   e dalla risoluzione scelta dall'utente.

B) Pixel-perfect con griglia manuale
   La griglia raster viene definita manualmente copiando dall'ortomosaico di
   riferimento:
     - width / larghezza in pixel
     - height / altezza in pixel
     - origin X = coordinata X dell'angolo alto-sinistra
     - origin Y = coordinata Y dell'angolo alto-sinistra
     - pixel size X
     - pixel size Y, normalmente negativo
   In questa modalita' l'output usa ESATTAMENTE la stessa griglia del raster di
   riferimento, senza dipendenze esterne dentro Metashape.

CARATTERISTICHE
---------------
- Proiezione planare con assi (u, v, w) definiti da 3 marker.
- Rasterizzazione a TRIANGOLO PIENO con z-buffer per-pixel (Python puro).
- Clipping esatto dei triangoli sul bounding box del chunk.
- Normali per-faccia, non smussate: ogni pixel = giacitura reale della faccetta.
- Normali forzate verso +w per evitare inversioni dovute all'ordine dei vertici.
- Output base: raster float32 a 3 bande con coseni direttori (n.u, n.v, n.w).
- Output opzionale: bande derivate.

OUTPUT
------
- Solo ENVI .dat + .hdr, leggibile direttamente in QGIS.
- Nessuna dipendenza da librerie esterne per lettura/scrittura raster.
- La modalita' B resta pixel-perfect purche' i parametri della griglia siano
  copiati correttamente dal raster di riferimento.

USO
---
Tools -> Run Script...
Richiede:
- mesh gia' costruita;
- almeno 3 marker con posizione 3D nel chunk attivo.
"""

import os
import math
from array import array
import Metashape

# Qt: Metashape espone PySide (2 o 6 a seconda della build). Proviamo entrambi.
QT = None
try:
    from PySide2 import QtWidgets, QtCore
    QT = "PySide2"
except ImportError:
    try:
        from PySide6 import QtWidgets, QtCore
        QT = "PySide6"
    except ImportError:
        QT = None

if QT is not None:
    try:
        QT_WINDOW_MODAL = QtCore.Qt.WindowModal
    except AttributeError:
        QT_WINDOW_MODAL = QtCore.Qt.WindowModality.WindowModal
    try:
        QT_ALIGN_HCENTER = QtCore.Qt.AlignHCenter
    except AttributeError:
        QT_ALIGN_HCENTER = QtCore.Qt.AlignmentFlag.AlignHCenter
    try:
        QT_BUTTON_OK = QtWidgets.QDialogButtonBox.Ok
        QT_BUTTON_CANCEL = QtWidgets.QDialogButtonBox.Cancel
    except AttributeError:
        QT_BUTTON_OK = QtWidgets.QDialogButtonBox.StandardButton.Ok
        QT_BUTTON_CANCEL = QtWidgets.QDialogButtonBox.StandardButton.Cancel

NAN = float("nan")
MAX_RASTER_PIXELS = 200000000
EPS_AXIS_NORM = 1e-10
EPS_GRID = 1e-12



# ============================================================
# RACCOLTA MARKER DISPONIBILI
# ============================================================
def get_chunk():
    chunk = Metashape.app.document.chunk
    if chunk is None:
        raise RuntimeError("Nessun chunk attivo.")
    if chunk.model is None:
        raise RuntimeError("Il chunk attivo non ha una mesh. Esegui prima Build Mesh.")
    if chunk.transform is None or chunk.transform.matrix is None:
        raise RuntimeError("Il chunk non ha una transform valida.")
    if chunk.region is None or chunk.region.size is None:
        raise RuntimeError("Il chunk non ha un bounding box (region) valido.")
    return chunk


def available_markers(chunk):
    """Lista (label, marker) dei soli marker con posizione 3D stimata."""
    out = []
    for m in chunk.markers:
        if m.position:
            out.append((m.label, m))
    return out


# ============================================================
# DIALOGO DI PROGRESSIONE
# ============================================================
if QT is not None:
    class CenteredProgressDialog(QtWidgets.QDialog):
        """Progress dialog semplice, con pulsante Annulla centrato sotto la barra."""
        def __init__(self, title, text, parent=None):
            super(CenteredProgressDialog, self).__init__(parent)
            self._cancelled = False
            self.setWindowTitle(title)
            self.setWindowModality(QT_WINDOW_MODAL)
            self.setMinimumWidth(460)
            self.setMinimumHeight(150)

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)

            self.label = QtWidgets.QLabel(text)
            self.label.setWordWrap(True)
            self.label.setMinimumWidth(410)
            layout.addWidget(self.label)

            self.bar = QtWidgets.QProgressBar()
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            layout.addWidget(self.bar)

            self.cancel_btn = QtWidgets.QPushButton("Annulla")
            self.cancel_btn.setMinimumWidth(120)
            self.cancel_btn.setMinimumHeight(28)
            self.cancel_btn.clicked.connect(self._on_cancel)
            layout.addWidget(self.cancel_btn, 0, QT_ALIGN_HCENTER)

        def _on_cancel(self):
            self._cancelled = True
            self.cancel_btn.setEnabled(False)
            self.label.setText("Annullamento in corso...")

        def setValue(self, value):
            self.bar.setValue(int(value))

        def setText(self, text):
            self.label.setText(text)

        def wasCanceled(self):
            return self._cancelled


# ============================================================
# WIDGET NUMERICI
# ============================================================
def make_float_spin(value=0.0, decimals=12, minimum=-1.0e12, maximum=1.0e12):
    spin = QtWidgets.QDoubleSpinBox()
    spin.setDecimals(decimals)
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    spin.setSingleStep(0.001)
    return spin


# ============================================================
# DIALOGO DI CONFIGURAZIONE
# ============================================================
class ConfigDialog(QtWidgets.QDialog):
    def __init__(self, markers, parent=None):
        super(ConfigDialog, self).__init__(parent)
        self.setWindowTitle("Ortomosaico normali - configurazione")
        self.markers = markers
        labels = [lbl for (lbl, _) in markers]

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "Scegli 3 marker (con posizione 3D) che definiscono il piano del muro.\n"
            "Origine: origine assi. Asse X: direzione orizzontale. "
            "Asse Y: direzione verticale, non collineare.\n\n"
            "Modalita' A: griglia automatica da bounding box + risoluzione.\n"
            "Modalita' B: griglia manuale per sovrapposizione pixel-perfect con un ortomosaico gia' esportato."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QtWidgets.QFormLayout()

        self.cmb_orig = QtWidgets.QComboBox(); self.cmb_orig.addItems(labels)
        self.cmb_x = QtWidgets.QComboBox(); self.cmb_x.addItems(labels)
        self.cmb_plane = QtWidgets.QComboBox(); self.cmb_plane.addItems(labels)
        if len(labels) >= 2:
            self.cmb_x.setCurrentIndex(1)
        if len(labels) >= 3:
            self.cmb_plane.setCurrentIndex(2)
        form.addRow("Origine:", self.cmb_orig)
        form.addRow("Asse X (orizzontale):", self.cmb_x)
        form.addRow("Asse Y (verticale):", self.cmb_plane)

        self.cmb_mode = QtWidgets.QComboBox()
        self.cmb_mode.addItems([
            "A - Automatica da mesh/region + risoluzione",
            "B - Pixel-perfect con griglia manuale",
        ])
        form.addRow("Modalita':", self.cmb_mode)

        self.spin_res = QtWidgets.QDoubleSpinBox()
        self.spin_res.setDecimals(5)
        self.spin_res.setRange(0.00001, 10.0)
        self.spin_res.setValue(0.001)
        self.spin_res.setSuffix(" m/px")
        form.addRow("Risoluzione A:", self.spin_res)

        # Parametri manuali modalita' B.
        self.spin_width = QtWidgets.QSpinBox()
        self.spin_width.setRange(1, 2000000)
        self.spin_width.setValue(1000)
        self.spin_height = QtWidgets.QSpinBox()
        self.spin_height.setRange(1, 2000000)
        self.spin_height.setValue(1000)
        self.spin_origin_x = make_float_spin(0.0)
        self.spin_origin_y = make_float_spin(0.0)
        self.spin_pix_x = make_float_spin(0.001, decimals=12, minimum=0.000000000001, maximum=1000000.0)
        self.spin_pix_y = make_float_spin(-0.001, decimals=12, minimum=-1000000.0, maximum=-0.000000000001)

        form.addRow("B width [px]:", self.spin_width)
        form.addRow("B height [px]:", self.spin_height)
        form.addRow("B origin X alto-sinistra:", self.spin_origin_x)
        form.addRow("B origin Y alto-sinistra:", self.spin_origin_y)
        form.addRow("B pixel size X:", self.spin_pix_x)
        form.addRow("B pixel size Y negativo:", self.spin_pix_y)

        self.manual_widgets = [
            self.spin_width, self.spin_height, self.spin_origin_x,
            self.spin_origin_y, self.spin_pix_x, self.spin_pix_y,
        ]
        self.cmb_mode.currentIndexChanged.connect(self._update_mode_widgets)

        path_row = QtWidgets.QHBoxLayout()
        self.edit_path = QtWidgets.QLineEdit(self._default_path())
        btn_browse = QtWidgets.QPushButton("...")
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self._browse_out)
        path_row.addWidget(self.edit_path)
        path_row.addWidget(btn_browse)
        form.addRow("Output:", path_row)

        self.chk_derived = QtWidgets.QCheckBox("Esporta anche bande derivate")
        form.addRow("", self.chk_derived)

        self.spin_thr = QtWidgets.QDoubleSpinBox()
        self.spin_thr.setDecimals(1)
        self.spin_thr.setRange(0.0, 89.0)
        self.spin_thr.setValue(8.0)
        self.spin_thr.setSuffix(" deg")
        self.spin_thr.setEnabled(False)
        form.addRow("Soglia aggetto (azimut):", self.spin_thr)
        self.chk_derived.toggled.connect(self.spin_thr.setEnabled)

        layout.addLayout(form)

        note = QtWidgets.QLabel(
            "Per la modalita' B copia i valori dal raster di riferimento. In QGIS: "
            "width/height dalle dimensioni raster; origin X = xmin; origin Y = ymax; "
            "pixel size X = risoluzione X; pixel size Y = -risoluzione Y.\n"
            "L'output e' sempre ENVI .dat + .hdr, senza usare librerie raster esterne."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        btns = QtWidgets.QDialogButtonBox(QT_BUTTON_OK | QT_BUTTON_CANCEL)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._update_mode_widgets(0)

    def _default_path(self):
        doc_path = Metashape.app.document.path
        base = os.path.dirname(doc_path) if doc_path else os.path.expanduser("~")
        return os.path.join(base, "normal_ortho_metric.dat")

    def _update_mode_widgets(self, index):
        manual = (index == 1)
        self.spin_res.setEnabled(not manual)
        for wdg in self.manual_widgets:
            wdg.setEnabled(manual)

    def _browse_out(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Salva raster normali", self.edit_path.text(),
            "ENVI raw (*.dat);;Tutti i file (*.*)")
        if path:
            self.edit_path.setText(path)

    def _on_ok(self):
        idxs = {self.cmb_orig.currentIndex(), self.cmb_x.currentIndex(), self.cmb_plane.currentIndex()}
        if len(idxs) < 3:
            QtWidgets.QMessageBox.warning(self, "Selezione non valida", "Devi scegliere tre marker DISTINTI.")
            return
        if not self.edit_path.text().strip():
            QtWidgets.QMessageBox.warning(self, "Percorso mancante", "Specifica un percorso di output.")
            return
        if self.cmb_mode.currentIndex() == 1:
            if self.spin_width.value() <= 0 or self.spin_height.value() <= 0:
                QtWidgets.QMessageBox.warning(self, "Griglia non valida", "Width e height devono essere maggiori di zero.")
                return
            if self.spin_pix_x.value() <= 0.0:
                QtWidgets.QMessageBox.warning(self, "Pixel size non valido", "Pixel size X deve essere positivo.")
                return
            if self.spin_pix_y.value() >= 0.0:
                QtWidgets.QMessageBox.warning(self, "Pixel size non valido", "Pixel size Y deve essere negativo.")
                return
            size = self.spin_width.value() * self.spin_height.value()
            if size > MAX_RASTER_PIXELS:
                QtWidgets.QMessageBox.warning(
                    self, "Raster troppo grande",
                    "La griglia manuale contiene {} pixel. Limite attuale: {} pixel.".format(size, MAX_RASTER_PIXELS))
                return
        self.accept()

    def values(self):
        return {
            "orig": self.markers[self.cmb_orig.currentIndex()][1],
            "axisx": self.markers[self.cmb_x.currentIndex()][1],
            "plane": self.markers[self.cmb_plane.currentIndex()][1],
            "mode": "manual_grid" if self.cmb_mode.currentIndex() == 1 else "auto",
            "res": self.spin_res.value(),
            "manual_width": self.spin_width.value(),
            "manual_height": self.spin_height.value(),
            "manual_origin_x": self.spin_origin_x.value(),
            "manual_origin_y": self.spin_origin_y.value(),
            "manual_pix_x": self.spin_pix_x.value(),
            "manual_pix_y": self.spin_pix_y.value(),
            "out": self.edit_path.text().strip(),
            "derived": self.chk_derived.isChecked(),
            "thr": self.spin_thr.value(),
        }


def ask_config(markers):
    parent = Metashape.app.findMainWindow() if hasattr(Metashape.app, "findMainWindow") else None
    dlg = ConfigDialog(markers, parent)
    if dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec():
        return dlg.values()
    return None


# ============================================================
# CORE: rasterizzazione + export
# ============================================================
def run(chunk, cfg):
    T = chunk.transform.matrix
    model = chunk.model

    m_orig = cfg["orig"]
    m_axisx = cfg["axisx"]
    m_plane = cfg["plane"]
    grid_mode = cfg.get("mode", "auto")
    res_cfg = cfg["res"]
    out_path = cfg["out"]
    export_derived = cfg["derived"]
    tang_dir_min_overhang_deg = cfg["thr"]

    # --- 1. piano dai marker ---
    P_orig = T.mulp(m_orig.position)
    P_horiz = T.mulp(m_axisx.position)
    P_plane = T.mulp(m_plane.position)

    cross = Metashape.Vector.cross
    ux = P_horiz - P_orig
    if ux.norm() < EPS_AXIS_NORM:
        raise RuntimeError("Origine e marker Asse X sono coincidenti o troppo vicini.")
    u = ux.normalized()

    vy = P_plane - P_orig
    vy_orth = vy - u * (vy * u)
    if vy_orth.norm() < EPS_AXIS_NORM:
        raise RuntimeError("I tre marker sono quasi collineari: impossibile definire il piano.")
    v = vy_orth.normalized()
    w = cross(u, v)
    if w.norm() < EPS_AXIS_NORM:
        raise RuntimeError("Assi locali degeneri: controlla la disposizione dei marker.")
    w = w.normalized()
    v = cross(w, u).normalized()

    # --- 1b. bounding box del chunk, in coordinate interne ---
    region = chunk.region
    R_center = region.center
    R_size = region.size
    R_rot = region.rot

    hx = R_size.x * 0.5
    hy = R_size.y * 0.5
    hz = R_size.z * 0.5

    bx = Metashape.Vector([R_rot[0, 0], R_rot[1, 0], R_rot[2, 0]]).normalized()
    by = Metashape.Vector([R_rot[0, 1], R_rot[1, 1], R_rot[2, 1]]).normalized()
    bz = Metashape.Vector([R_rot[0, 2], R_rot[1, 2], R_rot[2, 2]]).normalized()

    def box_local(Pint):
        d = Pint - R_center
        return (d * bx, d * by, d * bz)

    # --- 2. pre-calcolo vertici ---
    verts = model.vertices
    N = len(verts)
    Pproj = [None] * N
    Uc = [0.0] * N
    Vc = [0.0] * N
    Wc = [0.0] * N
    Bl = [None] * N

    for i in range(N):
        Vi = verts[i].coord
        P = T.mulp(Vi)
        Pproj[i] = P
        d = P - P_orig
        Uc[i] = d * u
        Vc[i] = d * v
        Wc[i] = d * w
        Bl[i] = box_local(Vi)

    faces = model.faces
    n_faces = len(faces)
    report_every = max(1, n_faces // 200)

    # ---- clipping contro i 6 piani del box ----
    def clip_against_axis(poly, axis, half, sign):
        if not poly:
            return poly
        out = []
        n = len(poly)
        for i in range(n):
            A = poly[i]
            B = poly[(i + 1) % n]
            da = sign * A[axis] - half
            db = sign * B[axis] - half
            a_in = da <= 0.0
            b_in = db <= 0.0
            if a_in:
                out.append(A)
            if a_in != b_in:
                denom = (db - da)
                if denom != 0.0:
                    t = -da / denom
                    out.append((A[0] + t * (B[0] - A[0]),
                                A[1] + t * (B[1] - A[1]),
                                A[2] + t * (B[2] - A[2])))
        return out

    def clip_triangle_to_box(p0, p1, p2):
        poly = [p0, p1, p2]
        poly = clip_against_axis(poly, 0, hx, 1.0)
        poly = clip_against_axis(poly, 0, hx, -1.0)
        if not poly:
            return poly
        poly = clip_against_axis(poly, 1, hy, 1.0)
        poly = clip_against_axis(poly, 1, hy, -1.0)
        if not poly:
            return poly
        poly = clip_against_axis(poly, 2, hz, 1.0)
        poly = clip_against_axis(poly, 2, hz, -1.0)
        return poly

    def boxlocal_to_uvw(s):
        Pint = R_center + bx * s[0] + by * s[1] + bz * s[2]
        P = T.mulp(Pint)
        d = P - P_orig
        return (d * u, d * v, d * w)

    def face_box_status(s0, s1, s2):
        outside = (
            (s0[0] > hx and s1[0] > hx and s2[0] > hx) or
            (s0[0] < -hx and s1[0] < -hx and s2[0] < -hx) or
            (s0[1] > hy and s1[1] > hy and s2[1] > hy) or
            (s0[1] < -hy and s1[1] < -hy and s2[1] < -hy) or
            (s0[2] > hz and s1[2] > hz and s2[2] > hz) or
            (s0[2] < -hz and s1[2] < -hz and s2[2] < -hz)
        )
        if outside:
            return False, False, False, False
        in0 = abs(s0[0]) <= hx and abs(s0[1]) <= hy and abs(s0[2]) <= hz
        in1 = abs(s1[0]) <= hx and abs(s1[1]) <= hy and abs(s1[2]) <= hz
        in2 = abs(s2[0]) <= hx and abs(s2[1]) <= hy and abs(s2[2]) <= hz
        return True, in0, in1, in2

    def mesh_intersects_region():
        for fi in range(n_faces):
            i0, i1, i2 = faces[fi].vertices
            s0 = Bl[i0]
            s1 = Bl[i1]
            s2 = Bl[i2]
            keep, in0, in1, in2 = face_box_status(s0, s1, s2)
            if not keep:
                continue
            if in0 and in1 and in2:
                return True
            poly = clip_triangle_to_box(s0, s1, s2)
            if len(poly) >= 3:
                return True
        return False

    # --- 3. definizione della griglia raster ---
    if grid_mode == "manual_grid":
        W = int(cfg["manual_width"])
        H = int(cfg["manual_height"])
        grid_origin_x = float(cfg["manual_origin_x"])
        grid_origin_y = float(cfg["manual_origin_y"])
        pix_x = float(cfg["manual_pix_x"])
        pix_y = float(cfg["manual_pix_y"])
        if pix_x <= EPS_GRID:
            raise RuntimeError("Modalita' B: pixel size X deve essere positivo.")
        if pix_y >= -EPS_GRID:
            raise RuntimeError("Modalita' B: pixel size Y deve essere negativo.")
        size = W * H
        if size > MAX_RASTER_PIXELS:
            raise RuntimeError(
                "Raster troppo grande: {} x {} = {} px. Limite attuale: {} px."
                .format(W, H, size, MAX_RASTER_PIXELS))
        if not mesh_intersects_region():
            raise RuntimeError(
                "Nessuna faccia della mesh interseca il bounding box del chunk. Controlla la region in Metashape.")
        geotransform = [grid_origin_x, pix_x, 0.0, grid_origin_y, 0.0, pix_y]
        print("Modalita' B - griglia manuale pixel-perfect:")
        print("  size: {} x {} px".format(W, H))
        print("  geotransform: {}".format(geotransform))
    else:
        umin = float("inf")
        umax = float("-inf")
        vmin = float("inf")
        vmax = float("-inf")
        any_clip = False
        for fi in range(n_faces):
            i0, i1, i2 = faces[fi].vertices
            s0 = Bl[i0]
            s1 = Bl[i1]
            s2 = Bl[i2]
            keep, in0, in1, in2 = face_box_status(s0, s1, s2)
            if not keep:
                continue
            if in0 and in1 and in2:
                uvw = ((Uc[i0], Vc[i0], Wc[i0]),
                       (Uc[i1], Vc[i1], Wc[i1]),
                       (Uc[i2], Vc[i2], Wc[i2]))
            else:
                poly = clip_triangle_to_box(s0, s1, s2)
                if len(poly) < 3:
                    continue
                uvw = [boxlocal_to_uvw(s) for s in poly]
            any_clip = True
            for uu, vv, ww in uvw:
                if uu < umin:
                    umin = uu
                if uu > umax:
                    umax = uu
                if vv < vmin:
                    vmin = vv
                if vv > vmax:
                    vmax = vv

        if not any_clip:
            raise RuntimeError(
                "Nessuna faccia della mesh interseca il bounding box del chunk. Controlla la region in Metashape.")

        W = int(math.ceil((umax - umin) / res_cfg)) + 1
        H = int(math.ceil((vmax - vmin) / res_cfg)) + 1
        size = W * H
        if size > MAX_RASTER_PIXELS:
            raise RuntimeError(
                "Raster troppo grande: {} x {} = {} px. Aumenta la risoluzione o riduci la region. "
                "Limite attuale: {} px.".format(W, H, size, MAX_RASTER_PIXELS))

        grid_origin_x = umin
        grid_origin_y = vmax
        pix_x = res_cfg
        pix_y = -res_cfg
        geotransform = [grid_origin_x, pix_x, 0.0, grid_origin_y, 0.0, pix_y]
        print("Modalita' A - griglia automatica:")
        print("  size: {} x {} px, GSD {} u/px".format(W, H, res_cfg))
        print("  geotransform: {}".format(geotransform))

    inv_pix_x = 1.0 / pix_x
    inv_pix_y = 1.0 / pix_y

    r_band = array('f', [NAN]) * size
    g_band = array('f', [NAN]) * size
    b_band = array('f', [NAN]) * size
    zbuf = array('d', [float("-inf")]) * size

    progress = None
    if QT is not None:
        try:
            parent = Metashape.app.findMainWindow() if hasattr(Metashape.app, "findMainWindow") else None
            progress = CenteredProgressDialog("Ortomosaico normali", "Rasterizzazione normali in corso...", parent)
            progress.setValue(0)
            progress.show()
            QtWidgets.QApplication.processEvents()
        except Exception:
            progress = None

    def raster_subtriangle(x0, y0, d0, x1, y1, d1, x2, y2, d2, rr, gg, bb_):
        minx = int(math.floor(min(x0, x1, x2)))
        maxx = int(math.ceil(max(x0, x1, x2)))
        miny = int(math.floor(min(y0, y1, y2)))
        maxy = int(math.ceil(max(y0, y1, y2)))
        if minx < 0:
            minx = 0
        if miny < 0:
            miny = 0
        if maxx > W - 1:
            maxx = W - 1
        if maxy > H - 1:
            maxy = H - 1
        if minx > maxx or miny > maxy:
            return
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-12:
            return
        inv_denom = 1.0 / denom
        ay = (y1 - y2)
        ax = (x2 - x1)
        by = (y2 - y0)
        bx2 = (x0 - x2)
        for py in range(miny, maxy + 1):
            cy = py + 0.5
            row = py * W
            dy_a = cy - y2
            for px in range(minx, maxx + 1):
                cx = px + 0.5
                dx = cx - x2
                a = (ay * dx + ax * dy_a) * inv_denom
                if a < 0.0:
                    continue
                bcoef = (by * dx + bx2 * dy_a) * inv_denom
                if bcoef < 0.0:
                    continue
                c = 1.0 - a - bcoef
                if c < 0.0:
                    continue
                depth = a * d0 + bcoef * d1 + c * d2
                idx = row + px
                if depth > zbuf[idx]:
                    zbuf[idx] = depth
                    r_band[idx] = rr
                    g_band[idx] = gg
                    b_band[idx] = bb_

    cancelled = False
    last_pct = -1
    for fi in range(n_faces):
        if fi % report_every == 0:
            ipct = int(100.0 * fi / n_faces)
            if ipct != last_pct:
                if progress is not None:
                    progress.setValue(ipct)
                    QtWidgets.QApplication.processEvents()
                    if progress.wasCanceled():
                        cancelled = True
                        break
                else:
                    print("  rasterizzazione: {:d}%".format(ipct))
                last_pct = ipct

        i0, i1, i2 = faces[fi].vertices

        P0 = Pproj[i0]
        P1 = Pproj[i1]
        P2 = Pproj[i2]
        nrm = cross(P1 - P0, P2 - P0)
        nn = nrm.norm()
        if nn == 0:
            continue
        nrm = nrm / nn
        if (nrm * w) < 0.0:
            nrm = nrm * -1.0
        rr = (nrm * u + 1.0) * 0.5
        gg = (nrm * v + 1.0) * 0.5
        bb_ = (nrm * w + 1.0) * 0.5

        s0 = Bl[i0]
        s1 = Bl[i1]
        s2 = Bl[i2]
        keep, in0, in1, in2 = face_box_status(s0, s1, s2)
        if not keep:
            continue

        if in0 and in1 and in2:
            x0 = (Uc[i0] - grid_origin_x) * inv_pix_x
            y0 = (Vc[i0] - grid_origin_y) * inv_pix_y
            d0 = Wc[i0]
            x1 = (Uc[i1] - grid_origin_x) * inv_pix_x
            y1 = (Vc[i1] - grid_origin_y) * inv_pix_y
            d1 = Wc[i1]
            x2 = (Uc[i2] - grid_origin_x) * inv_pix_x
            y2 = (Vc[i2] - grid_origin_y) * inv_pix_y
            d2 = Wc[i2]
            raster_subtriangle(x0, y0, d0, x1, y1, d1, x2, y2, d2, rr, gg, bb_)
        else:
            poly = clip_triangle_to_box(s0, s1, s2)
            if len(poly) < 3:
                continue
            uvw = [boxlocal_to_uvw(s) for s in poly]
            px = [(uu - grid_origin_x) * inv_pix_x for (uu, vv, ww) in uvw]
            py = [(vv - grid_origin_y) * inv_pix_y for (uu, vv, ww) in uvw]
            pw = [ww for (uu, vv, ww) in uvw]
            for t in range(1, len(poly) - 1):
                raster_subtriangle(px[0], py[0], pw[0],
                                   px[t], py[t], pw[t],
                                   px[t + 1], py[t + 1], pw[t + 1],
                                   rr, gg, bb_)

    if progress is not None:
        progress.setValue(100)
        progress.close()

    if cancelled:
        print("Operazione annullata dall'utente: nessun file scritto.")
        return

    print("  rasterizzazione: 100%")
    del zbuf

    # --- 4. coseni direttori + export ---
    def encode_inplace(band01):
        for k in range(size):
            val = band01[k]
            if val == val:
                band01[k] = val * 2.0 - 1.0
        return band01

    nx = encode_inplace(r_band)
    ny = encode_inplace(g_band)
    nz = encode_inplace(b_band)

    def envi_paths(base_path):
        root, ext = os.path.splitext(base_path)
        ext = ext.lower()
        if ext == ".dat":
            return base_path, root + ".hdr"
        if ext in (".tif", ".tiff", ".img", ".raw"):
            return root + ".dat", root + ".hdr"
        return base_path + ".dat", base_path + ".hdr"

    def write_envi(base_path, bands, descriptions, meta=None):
        dat_file, hdr_file = envi_paths(base_path)
        with open(dat_file, "wb") as f:
            for band in bands:
                f.write(band.tobytes())
        # ENVI map info: pixel di riferimento 1,1 = angolo alto-sinistra.
        map_info = "{Arbitrary, 1, 1, %r, %r, %r, %r, 0, units=Meters}" % (
            geotransform[0], geotransform[3], abs(geotransform[1]), abs(geotransform[5]))
        band_names = "{" + ", ".join(descriptions) + "}"
        hdr_lines = [
            "ENVI",
            "description = {Normal ortho - CRS locale piano muro}",
            "samples = " + str(W),
            "lines = " + str(H),
            "bands = " + str(len(bands)),
            "header offset = 0",
            "file type = ENVI Standard",
            "data type = 4",
            "interleave = bsq",
            "byte order = 0",
            "data ignore value = nan",
            "map info = " + map_info,
            "band names = " + band_names,
        ]
        if meta:
            for key in sorted(meta.keys()):
                safe_key = str(key).replace(" ", "_")
                safe_val = str(meta[key]).replace("\n", " ")
                hdr_lines.append("{} = {{{}}}".format(safe_key, safe_val))
        with open(hdr_file, "w") as f:
            f.write("\n".join(hdr_lines) + "\n")
        return dat_file, hdr_file

    axis_meta = {
        "AXIS_U": "{},{},{}".format(u.x, u.y, u.z),
        "AXIS_V": "{},{},{}".format(v.x, v.y, v.z),
        "AXIS_W": "{},{},{}".format(w.x, w.y, w.z),
        "ORIGIN_PROJECT": "{},{},{}".format(P_orig.x, P_orig.y, P_orig.z),
        "PIXEL_SIZE_X": str(pix_x),
        "PIXEL_SIZE_Y": str(pix_y),
        "GRID_ORIGIN_X": str(grid_origin_x),
        "GRID_ORIGIN_Y": str(grid_origin_y),
        "GRID_WIDTH": str(W),
        "GRID_HEIGHT": str(H),
        "GRID_MODE": grid_mode,
        "LOCAL_CRS": "u=asse orizzontale marker, v=verticale ortogonalizzata, w=normale piano",
        "NORMAL_ORIENTATION": "normali forzate verso +w",
    }
    cos_desc = [
        "n.u (coseno dir. orizzontale)",
        "n.v (coseno dir. verticale)",
        "n.w (coseno dir. normale al piano)",
    ]

    dat, hdr = write_envi(out_path, [nx, ny, nz], cos_desc, axis_meta)
    print("Salvato raster ENVI coseni direttori:")
    print("  dati  : {}".format(dat))
    print("  header: {}".format(hdr))

    # --- 5. bande derivate opzionali ---
    if export_derived:
        overhang = array('f', [NAN]) * size
        tang_dir = array('f', [NAN]) * size
        inclination_index = array('f', [NAN]) * size
        deg = math.degrees
        thr = tang_dir_min_overhang_deg
        n_masked = 0
        for k in range(size):
            z = nz[k]
            if z != z:
                continue
            az = abs(z)
            if az > 1.0:
                az = 1.0
            oh = deg(math.acos(az))
            overhang[k] = oh
            inclination_index[k] = 1.0 - az
            if oh < thr:
                n_masked += 1
            else:
                tang_dir[k] = (deg(math.atan2(ny[k], nx[k])) + 360.0) % 360.0
        print("tang_dir: {} px sotto {} deg di aggetto messi a NaN".format(n_masked, thr))

        der_desc = [
            "Angolo di inclinazione rispetto al piano [gradi]",
            "Azimut deviazione tangenziale [gradi 0-360]",
            "Indice di inclinazione (1-|n.w|)",
        ]
        root, ext = os.path.splitext(out_path)
        derived_base = root + "_derived" + (".dat" if ext.lower() == ".dat" else "")
        dat, hdr = write_envi(derived_base, [overhang, tang_dir, inclination_index], der_desc, axis_meta)
        print("Salvato raster ENVI bande derivate:")
        print("  dati  : {}".format(dat))
        print("  header: {}".format(hdr))

    print("Fatto.")


# ============================================================
# MAIN
# ============================================================
def main():
    chunk = get_chunk()
    markers = available_markers(chunk)
    if len(markers) < 3:
        raise RuntimeError(
            "Servono almeno 3 marker con posizione 3D nel chunk. Trovati: {}.".format(len(markers)))

    if QT is None:
        raise RuntimeError("PySide non disponibile: impossibile aprire il dialogo.")

    cfg = ask_config(markers)
    if cfg is None:
        print("Annullato.")
        return
    run(chunk, cfg)


main()
