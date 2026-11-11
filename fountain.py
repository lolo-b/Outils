#!/usr/bin/env python3
"""
Fontaine intelligente "chat uniquement" - service de détection (Raspberry Pi 4)

Ce service publie uniquement le résultat de la détection sur MQTT. L'activation et
l'arrêt de la fontaine sont gérés par homeserver.js (aucune gestion de pompe ici).

Flux :
  ESP32-CAM --MQTT fontaine/motion--> ce service
  ce service --HTTP GET /capture--> ESP32-CAM (rafale d'images)
  YOLOv8n local (ONNX Runtime, sans torch) --> MQTT  fontaine/detection

Format du message (texte) :
    cat=1,person=2,unrecognized=0
  - cat          : nombre de chats détectés
  - person       : nombre de personnes détectées
  - unrecognized : 1 si ni chat ni personne n'a été identifié, sinon 0

Règles :
  - un chat n'est compté que s'il est confirmé : vu sur CAT_HITS_REQUIRED images de la
    rafale, ou vu une seule fois avec une confiance >= CONF_CAT_FAST (publication immédiate)
  - les personnes n'ont pas besoin de confirmation (seuil bas, pour ne rien rater)
  - un message est publié par mouvement PIR analysé
"""

import json
import logging
import os
import queue
import signal
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import paho.mqtt.client as mqtt
import requests


def env(name, default, cast=str):
    return cast(os.environ.get(name, default))


# ============================ CONFIGURATION ============================
# (chaque valeur peut être surchargée par variable d'environnement)
CAM_URL        = env("CAM_URL", "http://fontaine-cam.local/capture")  # ou IP fixe: http://192.168.1.60/capture
MQTT_HOST      = env("MQTT_HOST", "localhost")
MQTT_PORT      = env("MQTT_PORT", 1883, int)
MQTT_USER      = env("MQTT_USER", "HomeServerMQTT")
MQTT_PASS      = env("MQTT_PASS", "P@sS#0M€2O22Acc€s")

TOPIC_MOTION     = "fontaine/motion"            # entrée : PIR (ESP32-CAM)
TOPIC_DETECTION  = "fontaine/detection"         # sortie : "cat=1,person=2,unrecognized=0"
TOPIC_DETAIL     = "fontaine/detection/detail"  # sortie : JSON (confiances, latence, ...)
TOPIC_DET_STATUS = "fontaine/detector/status"   # sortie : "online" / "offline" (LWT)

MODEL_PATH     = env("MODEL_PATH", "yolov8n.onnx")   # exporté sur PC avec imgsz=320
IMGSZ          = env("IMGSZ", 320, int)              # doit correspondre à l'export ONNX
ORT_THREADS    = env("ORT_THREADS", 4, int)
CLASS_PERSON   = 0
CLASS_CAT      = 15

CONF_PERSON    = env("CONF_PERSON", 0.40, float)    # bas : on ne veut rater aucune personne
CONF_CAT       = env("CONF_CAT", 0.50, float)
CONF_CAT_FAST  = env("CONF_CAT_FAST", 0.75, float)  # chat net : publication dès la 1re image (1.01 = désactivé)
NMS_IOU        = env("NMS_IOU", 0.45, float)        # fusion des boîtes qui se chevauchent
BURST_FRAMES   = env("BURST_FRAMES", 3, int)
CAT_HITS_REQUIRED = env("CAT_HITS_REQUIRED", 2, int)

COOLDOWN_SECONDS = env("COOLDOWN_SECONDS", 2, float)  # délai minimal entre deux analyses
CAM_TIMEOUT      = env("CAM_TIMEOUT", 2.0, float)

SAVE_DIR       = Path(env("SAVE_DIR", "/home/pi/Outils/data_foutain"))
SAVE_SAMPLES   = env("SAVE_SAMPLES", "1") == "1"            # images de la rafale (pour fine-tuning)
RETENTION_DAYS = env("RETENTION_DAYS", 14, int)
# =======================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("fontaine")

stop_event = threading.Event()
events: "queue.Queue[str]" = queue.Queue(maxsize=20)

Detection = namedtuple("Detection", "person cat person_conf cat_conf")


# ----------------------------- Caméra --------------------------------
class Camera:
    def __init__(self, url):
        self.url = url
        self.session = requests.Session()

    def grab_jpeg(self) -> bytes:
        r = self.session.get(self.url, timeout=CAM_TIMEOUT)
        r.raise_for_status()
        return r.content

    @staticmethod
    def decode(jpeg: bytes):
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("JPEG invalide")
        return img


# ----------------------------- Détecteur -----------------------------
class Detector:
    """YOLOv8 ONNX via onnxruntime (sans torch). Compte les personnes et les chats
    après suppression des boîtes en double (NMS par classe)."""

    def __init__(self, path):
        log.info("Chargement du modèle %s ...", path)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = ORT_THREADS
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        # warm-up : la 1re inférence est toujours lente
        dummy = np.zeros((480, 640, 3), np.uint8)
        for _ in range(2):
            self.detect(dummy)
        log.info("Modèle prêt")

    @staticmethod
    def _preprocess(img):
        """Letterbox (ratio conservé, bandes grises) -> RGB -> CHW float32 [0,1]."""
        h, w = img.shape[:2]
        r = min(IMGSZ / h, IMGSZ / w)
        nw, nh = int(round(w * r)), int(round(h * r))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((IMGSZ, IMGSZ, 3), 114, np.uint8)
        top, left = (IMGSZ - nh) // 2, (IMGSZ - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)  # BGR->RGB, HWC->CHW
        return (np.ascontiguousarray(blob, dtype=np.float32) / 255.0)[None]

    @staticmethod
    def _count(preds, cls, thr):
        """Nombre d'instances de la classe `cls` au-dessus du seuil, après NMS."""
        scores = preds[4 + cls]
        best = float(scores.max())
        idx = np.where(scores >= thr)[0]
        if idx.size == 0:
            return 0, best
        cx, cy, w, h = preds[0, idx], preds[1, idx], preds[2, idx], preds[3, idx]
        boxes = np.stack([cx - w / 2, cy - h / 2, w, h], axis=1).astype(np.float32).tolist()
        keep = cv2.dnn.NMSBoxes(boxes, scores[idx].astype(float).tolist(), thr, NMS_IOU)
        return len(keep), best

    def detect(self, img) -> Detection:
        out = self.sess.run(None, {self.input_name: self._preprocess(img)})[0]
        preds = out[0]                       # attendu : (84, N) = 4 coords + 80 classes
        if preds.shape[0] > preds.shape[1]:  # sécurité si l'export est transposé
            preds = preds.T
        n_person, person_conf = self._count(preds, CLASS_PERSON, CONF_PERSON)
        n_cat, cat_conf = self._count(preds, CLASS_CAT, CONF_CAT)
        return Detection(n_person, n_cat, person_conf, cat_conf)


# ----------------------------- MQTT ----------------------------------
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fontaine-pi")


def publish(topic, payload, retain=False, qos=0):
    try:
        return mqtt_client.publish(topic, payload, qos=qos, retain=retain)
    except Exception as e:
        log.warning("Publication MQTT échouée: %s", e)
        return None


def on_connect(client, userdata, flags, reason_code, properties):
    log.info("MQTT connecté (%s)", reason_code)
    client.subscribe([(TOPIC_MOTION, 0)])
    client.publish(TOPIC_DET_STATUS, "online", qos=1, retain=True)


def on_message(client, userdata, msg):
    if msg.topic == TOPIC_MOTION:
        try:
            events.put_nowait("motion")
        except queue.Full:
            pass


# ----------------------------- Données -------------------------------
def save_sample(jpeg: bytes, d: Detection):
    if not SAVE_SAMPLES:
        return
    try:
        now = datetime.now()
        folder = SAVE_DIR / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        name = (f"{now.strftime('%H%M%S_%f')[:-3]}_cat{d.cat}_person{d.person}"
                f"_c{d.cat_conf:.2f}_p{d.person_conf:.2f}.jpg")
        (folder / name).write_bytes(jpeg)
    except Exception as e:
        log.warning("Sauvegarde image échouée: %s", e)


def cleanup_old_samples():
    if not SAVE_DIR.exists():
        return
    limit = datetime.now() - timedelta(days=RETENTION_DAYS)
    for d in SAVE_DIR.iterdir():
        try:
            if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d") < limit:
                for f in d.glob("*"):
                    f.unlink()
                d.rmdir()
                log.info("Supprimé: %s", d)
        except ValueError:
            continue


# ----------------------------- Logique -------------------------------
class Controller:
    def __init__(self, cam, det):
        self.cam, self.det = cam, det
        self.cooldown_until = 0.0

    def emit(self, n_cat, n_person, **detail):
        unrecognized = 1 if (n_cat == 0 and n_person == 0) else 0
        payload = f"cat={n_cat},person={n_person},unrecognized={unrecognized}"
        data = {"ts": datetime.now().isoformat(timespec="seconds"),
                "cat": n_cat, "person": n_person, "unrecognized": unrecognized, **detail}
        log.info("Détection: %s  %s", payload, json.dumps(detail))
        publish(TOPIC_DETECTION, payload, qos=1)
        publish(TOPIC_DETAIL, json.dumps(data))

    def handle_motion(self):
        if time.monotonic() < self.cooldown_until:
            return
        try:
            self._analyse_burst()
        finally:
            self.cooldown_until = time.monotonic() + COOLDOWN_SECONDS

    def _analyse_burst(self):
        t0 = time.monotonic()
        max_cat = max_person = 0          # nombre max d'instances vues sur une même image
        best_cat = best_person = 0.0
        cat_frames = analysed = 0
        early = False

        for i in range(BURST_FRAMES):
            try:
                jpeg = self.cam.grab_jpeg()
                d = self.det.detect(self.cam.decode(jpeg))
            except Exception as e:
                log.warning("Image %d échouée: %s", i, e)
                continue

            analysed += 1
            save_sample(jpeg, d)
            max_person = max(max_person, d.person)
            best_person = max(best_person, d.person_conf)
            if d.cat > 0:
                cat_frames += 1
                max_cat = max(max_cat, d.cat)
                best_cat = max(best_cat, d.cat_conf)
                if d.cat_conf >= CONF_CAT_FAST:       # chat très net : inutile d'attendre
                    early = True
                    break
            if cat_frames >= CAT_HITS_REQUIRED:       # chat confirmé
                break

        ms = int((time.monotonic() - t0) * 1000)
        if analysed == 0:
            # rien d'analysable : on n'invente pas de détection
            log.warning("Aucune image exploitable")
            publish(TOPIC_DETAIL, json.dumps({"error": "capture_failed",
                    "ts": datetime.now().isoformat(timespec="seconds")}))
            return

        confirmed = early or cat_frames >= CAT_HITS_REQUIRED
        self.emit(max_cat if confirmed else 0, max_person,
                  cat_conf=round(best_cat, 2), person_conf=round(best_person, 2),
                  frames=analysed, early=early, ms=ms)


# ----------------------------- Main ----------------------------------
def main():
    cam = Camera(CAM_URL)
    det = Detector(MODEL_PATH)
    ctrl = Controller(cam, det)

    def shutdown(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.will_set(TOPIC_DET_STATUS, "offline", qos=1, retain=True)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=15)
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=15)
    mqtt_client.loop_start()

    cleanup_old_samples()
    last_cleanup = time.monotonic()
    log.info("Service prêt, en attente de mouvement")

    try:
        while not stop_event.is_set():
            try:
                events.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last_cleanup > 6 * 3600:
                    cleanup_old_samples()
                    last_cleanup = time.monotonic()
                continue
            ctrl.handle_motion()
            # purge des déclenchements PIR accumulés pendant le traitement
            while not events.empty():
                try:
                    events.get_nowait()
                except queue.Empty:
                    break
    finally:
        try:
            mqtt_client.publish(TOPIC_DET_STATUS, "offline", qos=1, retain=True).wait_for_publish(timeout=2)
        except Exception:
            pass
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        log.info("Arrêt propre")


if __name__ == "__main__":
    main()