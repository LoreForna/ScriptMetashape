import Metashape
from PySide2 import QtWidgets, QtCore

# ---------------------------------------------------------------------------
# Filtra le foto in base a una porzione selezionata del progetto e, a scelta,
# crea un nuovo chunk con le sole foto utili OPPURE disabilita in-place le foto
# non utili nel chunk corrente (utile per elaborare l'ortomosaico su un'area).
#
# SORGENTE DELLA SELEZIONE (rilevata automaticamente, in ordine di priorita'):
#   1. Tie point selezionati a mano -> filtraggio preciso per proiezioni.
#   2. Facce mesh selezionate a mano -> bounding box dei vertici -> tie point
#      interni al box -> foto. (model.faces/vertices sono leggibili via API.)
#   3. Region del chunk (fallback) -> tie point dentro il volume della Region.
#
# NOTA SULLA DENSE CLOUD: in Metashape 2.3 l'API Python NON espone i singoli
# punti della dense cloud, quindi una selezione fatta a mano sulla dense NON
# e' leggibile da script. Per lavorare "sull'area della dense": restringi la
# Region attorno alla zona (Resize Region nella toolbar) e lo script usera'
# il caso 3. La selezione via Region e' VOLUMETRICA (un box), non a forma
# libera: adatta a ritagliare un'area di interesse.
#
# MODALITA' DI OUTPUT (a scelta nella finestra):
#   A) Nuovo chunk  -> duplica il chunk, tiene solo le foto sopra soglia.
#   B) Disabilita in-place -> nel chunk corrente mette enabled=False alle foto
#      sotto soglia. Non duplica, non rimuove nulla, reversibile.
#
# SOGLIA: conteggio assoluto o percentuale (del max osservato), con anteprima.
#
# Compatibile con Metashape 2.3.x
# ---------------------------------------------------------------------------

compatible_major_version = "2.3"
found_major_version = ".".join(Metashape.app.version.split('.')[:2])
if found_major_version != compatible_major_version:
    raise Exception("Versione Metashape incompatibile: {} != {}".format(
        found_major_version, compatible_major_version))


def selected_tie_tracks(chunk):
    """Track_id dei tie point attualmente selezionati (o set vuoto)."""
    if not chunk.tie_points:
        return set()
    return {p.track_id for p in chunk.tie_points.points if p.selected}


def tracks_in_region(chunk):
    """
    Track_id dei tie point interni alla Region del chunk.
    In Metashape 2.3 l'API PointCloud NON espone i singoli punti densi in
    Python, quindi non e' possibile leggere una selezione fatta a mano sulla
    dense cloud. Si usa invece la Region come volume di interesse: l'utente la
    restringe attorno alla zona voluta (Resize Region nella toolbar), poi lo
    script filtra i tie point che cadono dentro quel volume.

    Ottimizzato: rot/center/size della Region e la matrice inversa vengono
    letti UNA volta fuori dal ciclo (non a ogni punto), riducendo di molto
    gli attraversamenti C++<->Python su nuvole sparse grandi.
    """
    tie = chunk.tie_points
    if tie is None:
        return set()
    region = chunk.region
    Rinv = region.rot.inv()   # inversa calcolata una sola volta
    C = region.center
    S = region.size
    hx, hy, hz = S[0] / 2.0, S[1] / 2.0, S[2] / 2.0

    tracks = set()
    add = tracks.add          # binding locale: evita lookup dell'attributo nel loop
    for p in tie.points:
        c = p.coord
        c.size = 3
        v = Rinv * (c - C)
        if -hx <= v[0] <= hx and -hy <= v[1] <= hy and -hz <= v[2] <= hz:
            add(p.track_id)
    return tracks


def bbox_from_model(chunk):
    """
    Bounding box (min,max in coord interne) dei vertici delle facce
    selezionate della mesh. model.faces e model.vertices SONO esposti in
    Python (a differenza dei punti densi), quindi la selezione a mano sulla
    mesh e' leggibile. Ritorna None se non c'e' mesh o nessuna faccia selez.
    """
    model = chunk.model
    if model is None:
        return None
    verts = model.vertices
    mn = Metashape.Vector([float('inf')] * 3)
    mx = Metashape.Vector([-float('inf')] * 3)
    found = False
    for f in model.faces:
        if not f.selected:
            continue
        for vi in f.vertices:
            c = verts[vi].coord
            c.size = 3
            for i in range(3):
                mn[i] = min(mn[i], c[i]); mx[i] = max(mx[i], c[i])
        found = True
    return (mn, mx) if found else None


def tracks_in_bbox(chunk, bbox):
    """Track_id dei tie point interni a un bbox allineato agli assi."""
    mn, mx = bbox
    mnx, mny, mnz = mn[0], mn[1], mn[2]
    mxx, mxy, mxz = mx[0], mx[1], mx[2]
    tracks = set()
    add = tracks.add
    for p in chunk.tie_points.points:
        c = p.coord
        c.size = 3
        if mnx <= c[0] <= mxx and mny <= c[1] <= mxy and mnz <= c[2] <= mxz:
            add(p.track_id)
    return tracks


def resolve_selection(chunk):
    """
    Determina i track_id target dalla sorgente di selezione disponibile.
    Ritorna (tracks, sorgente_str). Priorita':
      1. Tie point selezionati a mano  -> preciso.
      2. Facce mesh selezionate a mano -> bbox dei vertici -> tie point interni.
      3. Region del chunk (fallback)   -> tie point interni al volume.
    La dense cloud NON e' usabile come sorgente diretta: in Metashape 2.3
    l'API non espone i suoi punti in Python. Per lavorare "dalla dense",
    restringi la Region attorno alla zona e lo script usera' il caso 3.
    """
    tracks = selected_tie_tracks(chunk)
    if tracks:
        return tracks, "Tie point selezionati"

    bbox = bbox_from_model(chunk)
    if bbox:
        return tracks_in_bbox(chunk, bbox), "Mesh selezionata (bounding box)"

    tracks = tracks_in_region(chunk)
    if tracks:
        return tracks, "Region del chunk (volume)"

    return set(), None


class FilterDlg(QtWidgets.QDialog):

    def __init__(self, parent):
        QtWidgets.QDialog.__init__(self, parent)
        self.setWindowTitle("Filtra foto da selezione")
        self.setMinimumWidth(440)

        self.src = Metashape.app.document.chunk
        self.sel_tracks = set()
        self.source_str = None
        self.cam_counts = []      # (indice, count)
        self.max_count = 0
        self.error = None

        if not self.src or not self.src.tie_points:
            self.error = "Nessun chunk attivo o nessun tie point."
        else:
            self.sel_tracks, self.source_str = resolve_selection(self.src)
            if not self.sel_tracks:
                self.error = ("Nessuna selezione trovata.\n"
                              "Seleziona tie point o facce mesh, oppure "
                              "restringi la Region attorno alla zona.")
            else:
                self._precompute()

        # --- Widget ---
        self.srcLabel = QtWidgets.QLabel(
            "Sorgente: {}".format(self.source_str or "-"))
        self.infoLabel = QtWidgets.QLabel(
            "Tie point interessati: {}".format(len(self.sel_tracks)))

        # Modalita' soglia (gruppo esclusivo proprio)
        self.modeAbs = QtWidgets.QRadioButton("Conteggio assoluto")
        self.modePct = QtWidgets.QRadioButton("Percentuale")
        self.modeAbs.setChecked(True)
        self.modeGroup = QtWidgets.QButtonGroup(self)
        self.modeGroup.addButton(self.modeAbs)
        self.modeGroup.addButton(self.modePct)
        modeRow = QtWidgets.QHBoxLayout()
        modeRow.addWidget(self.modeAbs)
        modeRow.addWidget(self.modePct)

        self.thrSpin = QtWidgets.QSpinBox()
        self.thrSlider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        thrRow = QtWidgets.QHBoxLayout()
        thrRow.addWidget(QtWidgets.QLabel("Soglia:"))
        thrRow.addWidget(self.thrSlider)
        thrRow.addWidget(self.thrSpin)

        self.previewLabel = QtWidgets.QLabel()
        self.previewLabel.setStyleSheet("font-weight: bold;")

        # Modalita' di output (gruppo esclusivo proprio, separato dal precedente)
        self.outNew = QtWidgets.QRadioButton("Crea nuovo chunk (solo foto utili)")
        self.outDisable = QtWidgets.QRadioButton(
            "Disabilita in-place le foto non utili (chunk corrente)")
        self.outNew.setChecked(True)
        self.outGroup = QtWidgets.QButtonGroup(self)
        self.outGroup.addButton(self.outNew)
        self.outGroup.addButton(self.outDisable)
        outBox = QtWidgets.QVBoxLayout()
        outBox.addWidget(self.outNew)
        outBox.addWidget(self.outDisable)

        self.suffixEdit = QtWidgets.QLineEdit("_filtered")
        self.sufRow = QtWidgets.QHBoxLayout()
        self.sufRow.addWidget(QtWidgets.QLabel("Suffisso nuovo chunk:"))
        self.sufRow.addWidget(self.suffixEdit)

        self.runButton = QtWidgets.QPushButton("Esegui")
        self.closeButton = QtWidgets.QPushButton("Chiudi")
        btnRow = QtWidgets.QHBoxLayout()
        btnRow.addWidget(self.runButton)
        btnRow.addWidget(self.closeButton)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.srcLabel)
        layout.addWidget(self.infoLabel)
        layout.addSpacing(4)
        layout.addWidget(QtWidgets.QLabel("Modalita' soglia:"))
        layout.addLayout(modeRow)
        layout.addLayout(thrRow)
        layout.addWidget(self.previewLabel)
        layout.addSpacing(6)
        layout.addWidget(QtWidgets.QLabel("Output:"))
        layout.addLayout(outBox)
        layout.addLayout(self.sufRow)
        layout.addLayout(btnRow)
        self.setLayout(layout)

        self.modeAbs.toggled.connect(self.on_mode_changed)
        self.thrSlider.valueChanged.connect(self.on_slider)
        self.thrSpin.valueChanged.connect(self.on_spin)
        self.outNew.toggled.connect(self.on_output_changed)
        self.runButton.clicked.connect(self.run)
        self.closeButton.clicked.connect(self.close)

        if self.error:
            self.previewLabel.setText(self.error)
            for w in (self.runButton, self.thrSlider, self.thrSpin,
                      self.modeAbs, self.modePct, self.outNew, self.outDisable):
                w.setEnabled(False)
        else:
            self.on_mode_changed()
            self.on_output_changed()

    # -----------------------------------------------------------------
    def _precompute(self):
        # Binding locali: evitano lookup ripetuti di attributi nel loop caldo.
        tie = self.src.tie_points
        projections_map = tie.projections
        sel = self.sel_tracks
        counts = self.cam_counts
        max_count = 0
        for idx, cam in enumerate(self.src.cameras):
            projections = projections_map[cam]
            if projections is None:
                counts.append((idx, 0)); continue
            c = sum(1 for proj in projections if proj.track_id in sel)
            counts.append((idx, c))
            if c > max_count:
                max_count = c
        self.max_count = max_count

    def on_mode_changed(self):
        self.thrSlider.blockSignals(True); self.thrSpin.blockSignals(True)
        if self.modeAbs.isChecked():
            hi = max(1, self.max_count)
            self.thrSlider.setRange(1, hi); self.thrSpin.setRange(1, hi)
            d = min(20, hi)
            self.thrSlider.setValue(d); self.thrSpin.setValue(d)
            self.thrSpin.setSuffix(" punti")
        else:
            self.thrSlider.setRange(1, 100); self.thrSpin.setRange(1, 100)
            self.thrSlider.setValue(10); self.thrSpin.setValue(10)
            self.thrSpin.setSuffix(" %")
        self.thrSlider.blockSignals(False); self.thrSpin.blockSignals(False)
        self.update_preview()

    def on_slider(self, v):
        self.thrSpin.blockSignals(True); self.thrSpin.setValue(v)
        self.thrSpin.blockSignals(False); self.update_preview()

    def on_spin(self, v):
        self.thrSlider.blockSignals(True); self.thrSlider.setValue(v)
        self.thrSlider.blockSignals(False); self.update_preview()

    def on_output_changed(self):
        new_mode = self.outNew.isChecked()
        for i in range(self.sufRow.count()):
            w = self.sufRow.itemAt(i).widget()
            if w:
                w.setEnabled(new_mode)

    def current_min_count(self):
        v = self.thrSpin.value()
        if self.modeAbs.isChecked():
            return v
        return max(1, int(round(self.max_count * v / 100.0)))

    def kept_dropped_indices(self, thr):
        keep = [idx for idx, c in self.cam_counts if c >= thr]
        drop = [idx for idx, c in self.cam_counts if c < thr]
        return keep, drop

    def update_preview(self):
        if self.error:
            return
        thr = self.current_min_count()
        kept = sum(1 for _, c in self.cam_counts if c >= thr)
        total = len(self.cam_counts)
        if self.modePct.isChecked():
            self.previewLabel.setText(
                "Anteprima: {} / {} foto utili  (soglia = {}% -> almeno {} punti)".format(
                    kept, total, self.thrSpin.value(), thr))
        else:
            self.previewLabel.setText(
                "Anteprima: {} / {} foto utili  (almeno {} punti)".format(
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

        if self.outNew.isChecked():
            self._run_new_chunk(thr, keep_idx, drop_idx)
        else:
            self._run_disable_inplace(thr, keep_idx, drop_idx)

    def _run_new_chunk(self, thr, keep_idx, drop_idx):
        doc = Metashape.app.document
        suffix = self.suffixEdit.text() or "_filtered"

        chunk = self.src.copy()
        chunk.label = self.src.label + suffix

        for p in chunk.tie_points.points:
            if p.track_id not in self.sel_tracks:
                p.valid = False

        new_cams = chunk.cameras
        drop_new = [new_cams[i] for i in drop_idx if i < len(new_cams)]
        chunk.remove(drop_new)

        self._fit_region(chunk)
        doc.chunk = chunk

        QtWidgets.QMessageBox.information(
            self, "Fatto",
            "Nuovo chunk: '{}'\n"
            "Foto tenute: {}\nFoto rimosse: {}\n"
            "Soglia: almeno {} punti\n\n"
            "Allineamento conservato: puoi procedere con l'elaborazione.".format(
                chunk.label, len(keep_idx), len(drop_idx), thr))

    def _run_disable_inplace(self, thr, keep_idx, drop_idx):
        cams = self.src.cameras
        n_dis = 0
        for i in drop_idx:
            if i < len(cams):
                cams[i].enabled = False
                n_dis += 1
        n_en = 0
        for i in keep_idx:
            if i < len(cams):
                cams[i].enabled = True
                n_en += 1

        QtWidgets.QMessageBox.information(
            self, "Fatto",
            "Chunk corrente: '{}'\n"
            "Foto abilitate (utili): {}\n"
            "Foto disabilitate: {}\n"
            "Soglia: almeno {} punti\n\n"
            "Le foto disabilitate saranno ignorate nelle elaborazioni\n"
            "(es. Build Orthomosaic). Operazione reversibile: puoi\n"
            "riabilitarle dal pannello Photos.".format(
                self.src.label, n_en, n_dis, thr))

    def _fit_region(self, chunk):
        region = chunk.region
        R = region.rot; C = region.center
        mn = Metashape.Vector([float('inf')] * 3)
        mx = Metashape.Vector([-float('inf')] * 3)
        found = False
        for p in chunk.tie_points.points:
            if p.track_id not in self.sel_tracks:
                continue
            coord = p.coord; coord.size = 3
            v_r = R.inv() * (coord - C)
            mn = Metashape.Vector([min(mn[i], v_r[i]) for i in range(3)])
            mx = Metashape.Vector([max(mx[i], v_r[i]) for i in range(3)])
            found = True
        if not found:
            return
        region.center = C + R * ((mn + mx) / 2.0)
        region.size = mx - mn
        chunk.region = region


def launch():
    app = QtWidgets.QApplication.instance()
    parent = app.activeWindow()
    dlg = FilterDlg(parent)
    dlg.exec_()


label = "Scripts/Filter cameras by selection"
Metashape.app.addMenuItem(label, launch)
print("Per eseguire: menu {}".format(label))
