import Metashape
from PySide2 import QtWidgets, QtCore

# ---------------------------------------------------------------------------
# Filtra i tie point selezionati e crea un NUOVO chunk contenente solo le foto
# che contribuiscono alla porzione selezionata di nuvola sparsa.
#
# Workflow d'uso:
#   1. Vai sulla nuvola sparsa (Tie Points).
#   2. Con gli strumenti di selezione (Rectangle / Free-Form) seleziona i tie
#      point della zona che ti interessa.
#   3. Menu Scripts > "Filter cameras by selection to new chunk".
#   4. Scegli la modalita' (conteggio assoluto o percentuale), regola la
#      soglia guardando l'ANTEPRIMA che aggiorna il numero di foto tenute,
#      poi crea il nuovo chunk.
#
# Modalita' soglia:
#   - Conteggio assoluto: tiene le foto che vedono almeno N tie point selezionati.
#   - Percentuale: soglia espressa come % del massimo numero di tie point
#     selezionati osservati da una singola foto (max_count). Es. 10% con
#     max_count=200 -> tiene le foto che vedono almeno 20 punti selezionati.
#
# Cosa fa alla conferma:
#   - Duplica il chunk corrente (l'originale resta intatto).
#   - Nel duplicato tiene solo i tie point selezionati e restringe la region.
#   - Tiene solo le camere sopra soglia, rimuove le altre.
#   - L'allineamento resta valido: puoi passare subito a Build Point Cloud.
#
# Compatibile con Metashape 2.3.x
# ---------------------------------------------------------------------------

compatible_major_version = "2.3"
found_major_version = ".".join(Metashape.app.version.split('.')[:2])
if found_major_version != compatible_major_version:
    raise Exception("Versione Metashape incompatibile: {} != {}".format(
        found_major_version, compatible_major_version))


class FilterToNewChunkDlg(QtWidgets.QDialog):

    def __init__(self, parent):
        QtWidgets.QDialog.__init__(self, parent)
        self.setWindowTitle("Filtra selezione in nuovo chunk")
        self.setMinimumWidth(420)

        # --- Precalcolo: per ogni camera, quanti tie point selezionati vede ---
        self.src = Metashape.app.document.chunk
        self.sel_tracks = set()
        self.cam_counts = []   # lista di (camera, count)
        self.max_count = 0
        self.error = None

        if not self.src or not self.src.tie_points:
            self.error = "Nessun chunk attivo o nessun tie point."
        else:
            self.sel_tracks = {p.track_id for p in self.src.tie_points.points if p.selected}
            if not self.sel_tracks:
                self.error = "Nessun tie point selezionato."
            else:
                self._precompute()

        # --- Widget ---
        self.infoLabel = QtWidgets.QLabel(
            "Tie point selezionati: {}".format(len(self.sel_tracks)))

        # Modalita'
        self.modeAbs = QtWidgets.QRadioButton("Conteggio assoluto")
        self.modePct = QtWidgets.QRadioButton("Percentuale")
        self.modeAbs.setChecked(True)
        modeRow = QtWidgets.QHBoxLayout()
        modeRow.addWidget(self.modeAbs)
        modeRow.addWidget(self.modePct)

        # Slider soglia + spin
        self.thrLabel = QtWidgets.QLabel("Soglia:")
        self.thrSpin = QtWidgets.QSpinBox()
        self.thrSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        thrRow = QtWidgets.QHBoxLayout()
        thrRow.addWidget(self.thrLabel)
        thrRow.addWidget(self.thrSlider)
        thrRow.addWidget(self.thrSpin)

        # Anteprima
        self.previewLabel = QtWidgets.QLabel()
        self.previewLabel.setStyleSheet("font-weight: bold;")

        self.suffixLabel = QtWidgets.QLabel("Suffisso nuovo chunk:")
        self.suffixEdit = QtWidgets.QLineEdit("_filtered")
        sufRow = QtWidgets.QHBoxLayout()
        sufRow.addWidget(self.suffixLabel)
        sufRow.addWidget(self.suffixEdit)

        self.runButton = QtWidgets.QPushButton("Crea nuovo chunk")
        self.closeButton = QtWidgets.QPushButton("Chiudi")
        btnRow = QtWidgets.QHBoxLayout()
        btnRow.addWidget(self.runButton)
        btnRow.addWidget(self.closeButton)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.infoLabel)
        layout.addSpacing(6)
        layout.addWidget(QtWidgets.QLabel("Modalita' soglia:"))
        layout.addLayout(modeRow)
        layout.addLayout(thrRow)
        layout.addWidget(self.previewLabel)
        layout.addSpacing(6)
        layout.addLayout(sufRow)
        layout.addLayout(btnRow)
        self.setLayout(layout)

        # --- Connessioni ---
        self.modeAbs.toggled.connect(self.on_mode_changed)
        self.thrSlider.valueChanged.connect(self.on_slider)
        self.thrSpin.valueChanged.connect(self.on_spin)
        self.runButton.clicked.connect(self.run)
        self.closeButton.clicked.connect(self.close)

        if self.error:
            self.previewLabel.setText(self.error)
            self.runButton.setEnabled(False)
            self.thrSlider.setEnabled(False)
            self.thrSpin.setEnabled(False)
            self.modeAbs.setEnabled(False)
            self.modePct.setEnabled(False)
        else:
            self.on_mode_changed()

    # -----------------------------------------------------------------
    def _precompute(self):
        # Salviamo (indice_posizionale, count). L'indice e' stabile dopo .copy(),
        # a differenza della label che puo' essere duplicata tra piu' cartelle.
        tie = self.src.tie_points
        for idx, cam in enumerate(self.src.cameras):
            projections = tie.projections[cam]
            if projections is None:
                self.cam_counts.append((idx, 0))
                continue
            c = sum(1 for proj in projections if proj.track_id in self.sel_tracks)
            self.cam_counts.append((idx, c))
            if c > self.max_count:
                self.max_count = c

    # -----------------------------------------------------------------
    def on_mode_changed(self):
        # Riconfigura slider/spin in base alla modalita'
        self.thrSlider.blockSignals(True)
        self.thrSpin.blockSignals(True)
        if self.modeAbs.isChecked():
            hi = max(1, self.max_count)
            self.thrSlider.setMinimum(1)
            self.thrSlider.setMaximum(hi)
            self.thrSpin.setMinimum(1)
            self.thrSpin.setMaximum(hi)
            default = min(20, hi)
            self.thrSlider.setValue(default)
            self.thrSpin.setValue(default)
            self.thrSpin.setSuffix(" punti")
        else:
            self.thrSlider.setMinimum(1)
            self.thrSlider.setMaximum(100)
            self.thrSpin.setMinimum(1)
            self.thrSpin.setMaximum(100)
            self.thrSlider.setValue(10)
            self.thrSpin.setValue(10)
            self.thrSpin.setSuffix(" %")
        self.thrSlider.blockSignals(False)
        self.thrSpin.blockSignals(False)
        self.update_preview()

    def on_slider(self, v):
        self.thrSpin.blockSignals(True)
        self.thrSpin.setValue(v)
        self.thrSpin.blockSignals(False)
        self.update_preview()

    def on_spin(self, v):
        self.thrSlider.blockSignals(True)
        self.thrSlider.setValue(v)
        self.thrSlider.blockSignals(False)
        self.update_preview()

    # -----------------------------------------------------------------
    def current_min_count(self):
        """Restituisce la soglia in numero assoluto di punti, dato il modo."""
        v = self.thrSpin.value()
        if self.modeAbs.isChecked():
            return v
        # percentuale del massimo osservato
        return max(1, int(round(self.max_count * v / 100.0)))

    def count_kept(self):
        thr = self.current_min_count()
        return sum(1 for _, c in self.cam_counts if c >= thr), thr

    def kept_dropped_indices(self, thr):
        keep = [idx for idx, c in self.cam_counts if c >= thr]
        drop = [idx for idx, c in self.cam_counts if c < thr]
        return keep, drop

    def update_preview(self):
        if self.error:
            return
        kept, thr = self.count_kept()
        total = len(self.cam_counts)
        if self.modePct.isChecked():
            self.previewLabel.setText(
                "Anteprima: {} / {} foto tenute  (soglia = {}% -> almeno {} punti)".format(
                    kept, total, self.thrSpin.value(), thr))
        else:
            self.previewLabel.setText(
                "Anteprima: {} / {} foto tenute  (almeno {} punti)".format(
                    kept, total, thr))

    # -----------------------------------------------------------------
    def run(self):
        if self.error:
            return
        thr = self.current_min_count()
        keep_idx, drop_idx = self.kept_dropped_indices(thr)

        if not keep_idx:
            QtWidgets.QMessageBox.warning(
                self, "Nessuna foto",
                "Nessuna foto raggiunge la soglia. Abbassala e riprova.")
            return

        suffix = self.suffixEdit.text() or "_filtered"
        doc = Metashape.app.document

        # Duplica il chunk (l'originale resta intatto)
        chunk = self.src.copy()
        chunk.label = self.src.label + suffix

        # Invalida i tie point non selezionati
        for p in chunk.tie_points.points:
            if p.track_id not in self.sel_tracks:
                p.valid = False

        # Rimuovi le camere da scartare usando l'indice posizionale:
        # dopo .copy() l'ordine delle camere e' preservato, quindi
        # chunk.cameras[i] corrisponde a src.cameras[i]. Immune ai nomi duplicati.
        new_cams = chunk.cameras
        drop_new = [new_cams[i] for i in drop_idx if i < len(new_cams)]
        chunk.remove(drop_new)

        # Restringi la region al bounding box dei punti selezionati
        self.fit_region(chunk)

        doc.chunk = chunk

        QtWidgets.QMessageBox.information(
            self, "Fatto",
            "Nuovo chunk: '{}'\n"
            "Foto tenute: {}\n"
            "Foto rimosse: {}\n"
            "Soglia applicata: almeno {} punti\n\n"
            "Allineamento conservato: puoi procedere con Build Point Cloud.".format(
                chunk.label, len(keep_idx), len(drop_idx), thr))

    def fit_region(self, chunk):
        region = chunk.region
        R = region.rot
        C = region.center
        min_c = Metashape.Vector([float('inf')] * 3)
        max_c = Metashape.Vector([-float('inf')] * 3)
        found = False
        for p in chunk.tie_points.points:
            if p.track_id not in self.sel_tracks:
                continue
            coord = p.coord
            coord.size = 3
            v_r = R.t() * (coord - C)
            min_c = Metashape.Vector([min(min_c[i], v_r[i]) for i in range(3)])
            max_c = Metashape.Vector([max(max_c[i], v_r[i]) for i in range(3)])
            found = True
        if not found:
            return
        new_center = (min_c + max_c) / 2.0
        new_size = max_c - min_c
        region.center = C + R * new_center
        region.size = new_size
        chunk.region = region


def launch():
    app = QtWidgets.QApplication.instance()
    parent = app.activeWindow()
    dlg = FilterToNewChunkDlg(parent)
    dlg.exec_()


label = "Scripts/Filter cameras by selection to new chunk"
Metashape.app.addMenuItem(label, launch)
print("Per eseguire: menu {}".format(label))
