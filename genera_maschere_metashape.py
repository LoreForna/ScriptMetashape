#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genera_maschere_metashape.py

Genera maschere di sfondo in batch (GPU) per fotogrammetria in Agisoft Metashape.

Per ogni foto JPEG produce un PNG binario:
    - BIANCO (255) = oggetto da ricostruire
    - NERO  (0)   = sfondo da escludere
Questa e' la convenzione che Metashape si aspetta in:
    Tools -> Generate Masks... -> Method: From File

NAMING
------
Per ogni foto  IMG_1234.jpg  viene creato  IMG_1234_mask.png
nella cartella di output. In Metashape, nel dialog "From File", imposta:
    Filename template:  {filename}_mask.png
e come cartella quella di output qui sotto.

DIPENDENZE (sul TUO PC, in un venv o nell'ambiente OSGeo4W/conda che preferisci)
-------------------------------------------------------------------------------
All'avvio lo script CONTROLLA le dipendenze e, se mancano, propone di
installarle da solo via pip (rembg[gpu], onnxruntime-gpu, pillow, numpy; tqdm
opzionale). Per saltare la conferma usa l'opzione --yes.
Installazione manuale equivalente:
    pip install "rembg[gpu]" onnxruntime-gpu pillow numpy tqdm
Se onnxruntime-gpu non vede CUDA, ripiega automaticamente su CPU (piu' lento ma funziona).

USO
---
    python genera_maschere_metashape.py --input "C:/foto/conci" --output "C:/foto/conci/masks"

Opzioni utili:
    --model isnet-general-use   modello piu' preciso su oggetti generici (default: u2net)
    --alpha-threshold 10        soglia (0-255) sotto cui il pixel diventa sfondo
    --feather 0                 sfumatura bordi in px (0 = bordo netto, consigliato per maschere)
    --ext .jpg .jpeg .JPG       estensioni da processare
    --downscale 2000            lato lungo max usato per la segmentazione (poi la maschera
                                viene riportata alla risoluzione piena). Accelera molto su 24 MP
                                con perdita di precisione del bordo trascurabile per fotogrammetria.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# FIX DLL CUDA/cuDNN SU WINDOWS
# I pacchetti pip nvidia-*-cu12 mettono le DLL in sottocartelle di
# site-packages che NON sono nel PATH: onnxruntime non le trova e fallisce
# il caricamento del provider CUDA. Qui le registriamo esplicitamente con
# os.add_dll_directory() PRIMA di importare onnxruntime/rembg.
# ---------------------------------------------------------------------------
def register_cuda_dll_dirs(verbose=True):
    """
    Rende trovabili le DLL di cuDNN/CUDA (pacchetti pip nvidia-*-cu12) per
    onnxruntime su Windows. Fa DUE cose, perche' su onnxruntime-gpu recente
    os.add_dll_directory() da solo a volte non basta:
      1) registra ogni cartella con DLL via os.add_dll_directory();
      2) la antepone anche al PATH del processo.
    Individua la cartella base tramite nvidia.__path__ (metodo corretto per il
    namespace package, che non ha __file__).
    """
    if os.name != "nt":
        return  # serve solo su Windows

    bindirs = []
    # Metodo principale: usa nvidia.__path__
    try:
        import nvidia
        for base in list(getattr(nvidia, "__path__", [])):
            base = Path(base)
            # Sottocartelle con DLL: nvidia/<pkg>/bin (cudnn, cublas, cuda_runtime, ...)
            for sub in base.glob("*/bin"):
                if sub.is_dir():
                    bindirs.append(sub)
    except Exception:
        pass

    # Fallback: scandaglia le site-packages note
    if not bindirs:
        try:
            import site
            roots = []
            try:
                roots.extend(site.getsitepackages())
            except Exception:
                pass
            usp = site.getusersitepackages()
            if isinstance(usp, str):
                roots.append(usp)
            roots.append(str(Path(sys.executable).parent / "Lib" / "site-packages"))
            for root in roots:
                nv = Path(root) / "nvidia"
                if nv.is_dir():
                    for sub in nv.glob("*/bin"):
                        if sub.is_dir():
                            bindirs.append(sub)
        except Exception:
            pass

    # Deduplica preservando l'ordine
    seen = set()
    unique = []
    for d in bindirs:
        k = str(d).lower()
        if k not in seen:
            seen.add(k)
            unique.append(d)

    added = []
    for d in unique:
        # 1) DLL directory (Python 3.8+)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
        # 2) PATH del processo (in testa)
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        added.append(str(d))

    if verbose:
        if added:
            print("[i] DLL CUDA/cuDNN registrate (add_dll_directory + PATH):")
            for a in added:
                print("    " + a)
        else:
            print("[!] Nessuna cartella DLL nvidia-*-cu12 trovata. "
                  "Se la GPU non parte, installa: pip install nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12")


# ---------------------------------------------------------------------------
# BOOTSTRAP DIPENDENZE
# Controlla i pacchetti necessari e, se mancano, propone di installarli via pip.
# Mappa: nome_modulo_da_importare -> specifica_pip_da_installare
# ---------------------------------------------------------------------------
REQUIRED = {
    "numpy": "numpy",
    "PIL": "pillow",
    "onnxruntime": "onnxruntime-gpu",
    "rembg": "rembg[gpu]",
}
# Su Windows, onnxruntime-gpu ha bisogno di cuDNN 9 e del runtime CUDA 12.
# Li aggiungiamo come dipendenze cosi' vengono installati in automatico e la
# GPU funziona senza interventi manuali. (Su Linux di solito non servono qui.)
if os.name == "nt":
    REQUIRED["nvidia.cudnn"] = "nvidia-cudnn-cu12"
    REQUIRED["nvidia.cuda_runtime"] = "nvidia-cuda-runtime-cu12"

OPTIONAL = {
    "tqdm": "tqdm",  # barra di avanzamento, non indispensabile
}


def _missing(mapping):
    result = []
    for mod, spec in mapping.items():
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            # find_spec puo' sollevare se il pacchetto genitore non esiste
            # (es. 'nvidia.cudnn' quando 'nvidia' non e' installato)
            found = False
        if not found:
            result.append((mod, spec))
    return result


def _pip_install(specs):
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *specs]
    print("[pip]", " ".join(cmd))
    return subprocess.call(cmd) == 0


def ensure_dependencies(assume_yes=False):
    """
    Verifica le dipendenze richieste e, se mancano, chiede conferma (o procede
    se assume_yes) e le installa via pip. Termina lo script se restano assenti.
    Le dipendenze opzionali vengono offerte ma non bloccano l'esecuzione.
    """
    missing = _missing(REQUIRED)
    if missing:
        names = ", ".join(spec for _, spec in missing)
        print("\n[!] Mancano alcune librerie necessarie:")
        print("    " + names)
        print("    onnxruntime-gpu e rembg sono pesanti (centinaia di MB);")
        print("    il primo download del modello rembg richiede connessione.\n")
        if not assume_yes:
            resp = input("Installarle ora con pip? [S/n]: ").strip().lower()
            if resp not in ("", "s", "si", "sì", "y", "yes"):
                sys.exit("Operazione annullata. Installa manualmente con:\n"
                         '    pip install "rembg[gpu]" onnxruntime-gpu pillow numpy')
        ok = _pip_install([spec for _, spec in missing])
        still = _missing(REQUIRED)
        if not ok or still:
            ancora = ", ".join(spec for _, spec in still) if still else "(errore pip)"
            sys.exit(f"[X] Installazione non riuscita. Ancora mancanti: {ancora}\n"
                     'Prova manualmente:  pip install "rembg[gpu]" onnxruntime-gpu pillow numpy')
        print("[OK] Dipendenze installate.\n")

    # Opzionali: offri l'installazione ma non bloccare
    opt_missing = _missing(OPTIONAL)
    if opt_missing and not assume_yes:
        names = ", ".join(spec for _, spec in opt_missing)
        resp = input(f"Installare anche le opzionali ({names})? [S/n]: ").strip().lower()
        if resp in ("", "s", "si", "sì", "y", "yes"):
            _pip_install([spec for _, spec in opt_missing])
    elif opt_missing and assume_yes:
        _pip_install([spec for _, spec in opt_missing])


def parse_args():
    p = argparse.ArgumentParser(description="Genera maschere di sfondo per Metashape (GPU).")
    p.add_argument("--input", required=True, help="Cartella con le foto JPEG.")
    p.add_argument("--output", required=True, help="Cartella di destinazione delle maschere PNG.")
    p.add_argument("--model", default=None,
                   help="Modello rembg. Se omesso, lo script mostra un menu numerato all'avvio. "
                        "Es.: u2net, isnet-general-use, u2netp, birefnet-general.")
    p.add_argument("--ext", nargs="+", default=[".jpg", ".jpeg", ".JPG", ".JPEG"],
                   help="Estensioni immagine da processare.")
    p.add_argument("--alpha-threshold", type=int, default=10,
                   help="Soglia 0-255: alpha sotto questo valore -> sfondo (nero).")
    p.add_argument("--feather", type=int, default=0,
                   help="Sfumatura bordi in px (0 = bordo netto).")
    p.add_argument("--downscale", type=int, default=0,
                   help="Lato lungo max per la segmentazione (0 = risoluzione piena).")
    p.add_argument("--overwrite", action="store_true",
                   help="Sovrascrive maschere gia' esistenti.")
    p.add_argument("--suffix", default="_mask",
                   help="Suffisso del file maschera (default: _mask -> IMG_1234_mask.png).")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Installa le dipendenze mancanti senza chiedere conferma.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Catalogo modelli rembg con vantaggi/limiti, ordinati per utilita' nel
# rilievo fotogrammetrico di reperti / conci / oggetti su sfondo non neutro.
# ---------------------------------------------------------------------------
MODELS = [
    {
        "id": "isnet-general-use",
        "nome": "ISNet General Use",
        "pro": "Bordi netti e precisi; di solito il migliore stacco su oggetti generici "
               "(pietra, ceramica, metallo) anche con sfondo poco contrastato.",
        "contro": "Leggermente piu' lento di u2net; download ~170 MB al primo uso.",
        "consiglio": "Prima scelta consigliata per conci/reperti.",
    },
    {
        "id": "u2net",
        "nome": "U2-Net (generalista)",
        "pro": "Buon compromesso qualita'/velocita'; robusto e molto collaudato.",
        "contro": "Bordi a volte piu' morbidi; puo' lasciare aloni se l'oggetto si "
                  "confonde con lo sfondo.",
        "consiglio": "Default storico, valido se ISNet dovesse fallire su qualche scatto.",
    },
    {
        "id": "birefnet-general",
        "nome": "BiRefNet (general)",
        "pro": "Qualita' dei bordi spesso superiore (dettagli fini, spigoli); ottimo su "
               "soggetti complessi.",
        "contro": "Modello grande e piu' pesante sulla VRAM; piu' lento. Download ~900 MB. "
                  "Verifica che la tua versione di rembg lo includa.",
        "consiglio": "Da provare se vuoi il massimo dettaglio al bordo e la 4060 regge.",
    },
    {
        "id": "u2netp",
        "nome": "U2-Net P (leggero)",
        "pro": "Molto veloce e leggero sulla memoria.",
        "contro": "Meno preciso: bordi piu' grossolani, piu' errori su oggetti piccoli.",
        "consiglio": "Solo se hai tantissime foto e la velocita' conta piu' della precisione.",
    },
    {
        "id": "u2net_human_seg",
        "nome": "U2-Net Human Seg",
        "pro": "Ottimizzato per silhouette umane.",
        "contro": "Inadatto a oggetti inanimati: NON usarlo per reperti/conci.",
        "consiglio": "Ignoralo per il rilievo archeologico.",
    },
]


def scegli_modello_interattivo():
    """Mostra un menu numerato e ritorna l'id del modello scelto."""
    default = MODELS[0]
    print("\n" + "=" * 70)
    print("  MODELLI DISPONIBILI  (digita il numero e premi Invio)")
    print(f"  Modello di DEFAULT: {default['nome']}  (id: {default['id']})")
    print("=" * 70)
    for i, m in enumerate(MODELS, start=1):
        default_tag = "  [DEFAULT]" if i == 1 else ""
        print(f"\n  {i}) {m['nome']}  ->  id: {m['id']}{default_tag}")
        print(f"     + {m['pro']}")
        print(f"     - {m['contro']}")
        print(f"     => {m['consiglio']}")
    print("\n" + "=" * 70)

    while True:
        scelta = input(f"Modello [1-{len(MODELS)}], Invio = default ({default['id']}): ").strip()
        if scelta == "":
            print(f"[i] Uso il modello di default: {default['nome']} ({default['id']})")
            return default["id"]
        if scelta.isdigit() and 1 <= int(scelta) <= len(MODELS):
            return MODELS[int(scelta) - 1]["id"]
        print("  Scelta non valida, riprova.")


def check_providers():
    """Riporta se ONNX Runtime vede la GPU CUDA."""
    try:
        import onnxruntime as ort
        provs = ort.get_available_providers()
        if "CUDAExecutionProvider" in provs:
            print("[OK] CUDAExecutionProvider disponibile: la segmentazione girera' su GPU.")
        else:
            print("[!] CUDA non rilevata da onnxruntime. Si usera' la CPU (piu' lento).")
            print("    Providers disponibili:", provs)
        return provs
    except Exception as e:
        print("[!] Impossibile interrogare onnxruntime:", e)
        return []


def main():
    args = parse_args()

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    if not in_dir.is_dir():
        sys.exit(f"Cartella input inesistente: {in_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Verifica/installa le dipendenze PRIMA di importarle
    ensure_dependencies(assume_yes=args.yes)

    # Registra le cartelle DLL di cuDNN/CUDA (nvidia-*-cu12) PRIMA di importare
    # onnxruntime, altrimenti su Windows il provider CUDA non trova cudnn64_9.dll.
    register_cuda_dll_dirs()

    # Import differiti: solo ora che siamo certi siano installate
    global np, Image, tqdm
    import numpy as np
    from PIL import Image
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(x, **k):
            return x

    check_providers()

    # Import rembg dopo il check (l'import inizializza il runtime)
    try:
        from rembg import new_session, remove
    except ImportError:
        sys.exit('rembg non installato. Esegui:  pip install "rembg[gpu]" onnxruntime-gpu pillow numpy tqdm')

    # Se l'utente non ha passato --model, mostra il menu interattivo
    if args.model is None:
        args.model = scegli_modello_interattivo()

    print(f"[i] Modello: {args.model}")
    session = new_session(args.model)

    exts = {e.lower() for e in args.ext}
    files = sorted(p for p in in_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in exts)
    if not files:
        sys.exit(f"Nessuna immagine con estensioni {sorted(exts)} in {in_dir}")

    print(f"[i] Trovate {len(files)} immagini. Output in: {out_dir}")

    done, skipped = 0, 0
    for img_path in tqdm(files, desc="Maschere", unit="img"):
        out_path = out_dir / f"{img_path.stem}{args.suffix}.png"
        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue

        img = Image.open(img_path).convert("RGB")
        full_size = img.size  # (w, h)

        # Downscale opzionale per velocizzare la segmentazione su 24 MP
        work = img
        if args.downscale and max(full_size) > args.downscale:
            scale = args.downscale / max(full_size)
            new_size = (round(full_size[0] * scale), round(full_size[1] * scale))
            work = img.resize(new_size, Image.LANCZOS)

        # rembg con alpha matting per bordi piu' puliti
        cut = remove(
            work,
            session=session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10,
        )
        alpha = cut.split()[-1]  # canale alpha

        # Riporta alla risoluzione piena se downscalato
        if alpha.size != full_size:
            alpha = alpha.resize(full_size, Image.LANCZOS)

        a = np.asarray(alpha, dtype=np.uint8)
        # Binarizza: oggetto bianco, sfondo nero
        mask = np.where(a >= args.alpha_threshold, 255, 0).astype(np.uint8)
        mask_img = Image.fromarray(mask, mode="L")

        # Sfumatura bordi opzionale
        if args.feather > 0:
            from PIL import ImageFilter
            mask_img = mask_img.filter(ImageFilter.GaussianBlur(args.feather))

        mask_img.save(out_path)
        done += 1

    print(f"\n[FATTO] Maschere generate: {done} | saltate (gia' esistenti): {skipped}")
    print("In Metashape:  Tools -> Generate Masks -> Method: From File")
    print(f"  Cartella maschere: {out_dir}")
    print(f"  Template nome file: {{filename}}{args.suffix}.png")
    print("Poi, nell'allineamento, spunta:  Apply masks to -> Key points")


if __name__ == "__main__":
    main()
