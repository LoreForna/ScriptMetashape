# -*- coding: utf-8 -*-
"""
imposta_region_da_marker.py

Imposta POSIZIONE e ORIENTAMENTO della region (bounding box) del chunk attivo
a partire da 3 marker gia' presenti nel progetto, scelti da menu a tendina.

Coerente con Build_normal_map.py:
    - u = asse orizzontale     (origine -> marker Asse X)
    - v = asse verticale       (ortogonalizzato rispetto a u)
    - w = normale al piano     (u x v)

REGOLE DI COSTRUZIONE
---------------------
- Centro region  = punto medio tra marker origine e marker Asse X.
- Rotazione      = matrice [u | v | w] espressa in coordinate INTERNE del chunk.
- Dimensione     = LINEA lungo l'asse X: lunghezza su u = distanza tra Origine e
                   Asse X; spessore su v e w = valore iniziale minimo impostato
                   dall'utente, poi ingrossabile a mano trascinando le maniglie
                   della region in Metashape.

NOTE
----
La region in Metashape vive nel sistema di coordinate INTERNO del chunk
(pre-transform). Gli assi dei marker vengono quindi riportati in coordinate
interne tramite l'inversa di chunk.transform.matrix.

USO
---
Tools -> Run Script...
Richiede:
- almeno 3 marker con posizione 3D nel chunk attivo;
- (per la dimensione automatica) una mesh gia' costruita.
"""

import math
import re
import Metashape

# Qt: Metashape espone PySide (2 o 6 a seconda della build).
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
        QT_BUTTON_OK = QtWidgets.QDialogButtonBox.Ok
        QT_BUTTON_CANCEL = QtWidgets.QDialogButtonBox.Cancel
    except AttributeError:
        QT_BUTTON_OK = QtWidgets.QDialogButtonBox.StandardButton.Ok
        QT_BUTTON_CANCEL = QtWidgets.QDialogButtonBox.StandardButton.Cancel

EPS_AXIS_NORM = 1e-10


# ============================================================
# ACCESSO AL CHUNK E AI MARKER
# ============================================================
def get_chunk():
    chunk = Metashape.app.document.chunk
    if chunk is None:
        raise RuntimeError("Nessun chunk attivo.")
    if chunk.transform is None or chunk.transform.matrix is None:
        raise RuntimeError("Il chunk non ha una transform valida.")
    if chunk.region is None:
        raise RuntimeError("Il chunk non ha una region.")
    return chunk


def _natural_key(label):
    """Chiave di ordinamento alfanumerico 'naturale': separa testo e numeri
    cosi' che 'point 2' venga prima di 'point 10'."""
    parts = re.split(r"(\d+)", label)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((1, int(p), ""))
        else:
            key.append((0, 0, p.lower()))
    return key


def available_markers(chunk):
    """Lista (label, marker) dei soli marker con posizione 3D stimata,
    ordinata in modo alfanumerico naturale sul label."""
    markers = [(m.label, m) for m in chunk.markers if m.position]
    markers.sort(key=lambda pair: _natural_key(pair[0]))
    return markers


# ============================================================
# DIALOGO DI CONFIGURAZIONE
# ============================================================
class ConfigDialog(QtWidgets.QDialog):
    def __init__(self, markers, parent=None):
        super(ConfigDialog, self).__init__(parent)
        self.setWindowTitle("Imposta region da 3 marker")
        self.markers = markers
        labels = [lbl for (lbl, _) in markers]

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "Scegli 3 marker (con posizione 3D) che definiscono il piano di proiezione.\n"
            "Origine: primo estremo dell'asse orizzontale.\n"
            "Asse X: secondo estremo dell'asse orizzontale.\n"
            "Asse Y: definisce la direzione verticale (non collineare).\n\n"
            "Il CENTRO della region viene posto nel punto medio tra Origine e Asse X.\n"
            "La ROTAZIONE segue gli assi u (orizzontale), v (verticale), w (normale).\n\n"
            "La region diventa un SEGMENTO sul piano dei 3 marker: lunghezza =\n"
            "distanza Origine <-> Asse X su u, altezza = proiezione del marker Asse Y\n"
            "su v, spessore minimo su w. Poi la ingrossi in profondita' a mano\n"
            "trascinando le maniglie in Metashape."
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

        layout.addLayout(form)

        btns = QtWidgets.QDialogButtonBox(QT_BUTTON_OK | QT_BUTTON_CANCEL)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_ok(self):
        idxs = {self.cmb_orig.currentIndex(),
                self.cmb_x.currentIndex(),
                self.cmb_plane.currentIndex()}
        if len(idxs) < 3:
            QtWidgets.QMessageBox.warning(
                self, "Selezione non valida", "Devi scegliere tre marker DISTINTI.")
            return
        self.accept()

    def values(self):
        return {
            "orig": self.markers[self.cmb_orig.currentIndex()][1],
            "axisx": self.markers[self.cmb_x.currentIndex()][1],
            "plane": self.markers[self.cmb_plane.currentIndex()][1],
        }


def ask_config(markers):
    parent = Metashape.app.findMainWindow() if hasattr(Metashape.app, "findMainWindow") else None
    dlg = ConfigDialog(markers, parent)
    ok = dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
    return dlg.values() if ok else None


# ============================================================
# CORE
# ============================================================
def run(chunk, cfg):
    T = chunk.transform.matrix
    Tinv = T.inv()
    cross = Metashape.Vector.cross

    m_orig = cfg["orig"]
    m_axisx = cfg["axisx"]
    m_plane = cfg["plane"]

    # --- posizioni marker in coordinate di progetto (world) ---
    P_orig = T.mulp(m_orig.position)
    P_horiz = T.mulp(m_axisx.position)
    P_plane = T.mulp(m_plane.position)

    # --- assi ortonormali u/v/w (stessa logica di Build_normal_map) ---
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

    # --- centro provvisorio: punto medio Origine <-> Asse X ---
    # (verra' spostato al baricentro del rettangolo dopo il calcolo delle dimensioni)
    center_world = (P_orig + P_horiz) * 0.5

    # --- rotazione region: gli assi u/v/w vanno espressi in coord INTERNE ---
    # Solo la parte direzionale della transform (senza traslazione).
    u_int = Tinv.mulv(u).normalized()
    v_int = Tinv.mulv(v).normalized()
    w_int = Tinv.mulv(w).normalized()

    R = Metashape.Matrix([
        [u_int.x, v_int.x, w_int.x],
        [u_int.y, v_int.y, w_int.y],
        [u_int.z, v_int.z, w_int.z],
    ])

    region = chunk.region

    # --- dimensione: RETTANGOLO sul piano dei 3 marker, spessore minimo su w ---
    # lunghezza su u = distanza world tra Origine e Asse X;
    # altezza  su v = proiezione del marker Y (P_plane) sull'asse v verticale;
    # spessore su w = minimo assoluto per non degenerare la region.
    length_u = (P_horiz - P_orig).norm()
    height_v = abs((P_plane - P_orig) * v)
    if height_v < EPS_AXIS_NORM:
        raise RuntimeError("Il marker Asse Y non definisce un'altezza valida sul piano.")

    # spessore minimo: frazione piccolissima della diagonale, con floor fisso
    # per non arrivare mai a zero (Metashape non gestisce region degeneri).
    diag = math.sqrt(length_u * length_u + height_v * height_v)
    thickness = max(diag * 1e-4, 1e-6)

    # --- centro = baricentro del rettangolo ---
    # dall'origine: mezza lunghezza lungo u, meta' altezza lungo v (verso il marker Y).
    v_sign = 1.0 if ((P_plane - P_orig) * v) >= 0.0 else -1.0
    center_world = P_orig + u * (length_u * 0.5) + v * (v_sign * height_v * 0.5)
    center_int = Tinv.mulp(center_world)

    # scala della transform (assunta uniforme): la region usa coord INTERNE,
    # quindi le dimensioni world vanno divise per lo scale.
    scale = T.mulv(Metashape.Vector([1, 0, 0])).norm()
    if scale < EPS_AXIS_NORM:
        scale = 1.0

    size_int = Metashape.Vector([
        length_u / scale,
        height_v / scale,
        thickness / scale,
    ])
    region.size = size_int
    print("Region come rettangolo sul piano dei marker (world):")
    print("  lunghezza u: {:.6f} m".format(length_u))
    print("  altezza  v: {:.6f} m".format(height_v))
    print("  spessore w: {:.6e} m".format(thickness))

    region.center = center_int
    region.rot = R
    chunk.region = region

    print("Region aggiornata.")
    print("  centro (interno): {}".format(center_int))
    print("  asse u (world): {:.4f}, {:.4f}, {:.4f}".format(u.x, u.y, u.z))
    print("  asse v (world): {:.4f}, {:.4f}, {:.4f}".format(v.x, v.y, v.z))
    print("  asse w (world): {:.4f}, {:.4f}, {:.4f}".format(w.x, w.y, w.z))
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
