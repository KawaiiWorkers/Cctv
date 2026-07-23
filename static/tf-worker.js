// Optional Web Worker for off-main-thread COCO-SSD detection.
// Loaded via `new Worker('/static/tf-worker.js')` if you wish to
// offload detection from the UI thread on heavy desktops.
importScripts(
  "https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@4.17.0/dist/tf.min.js",
  "https://cdn.jsdelivr.net/npm/@tensorflow-models/coco-ssd@2.2.3/dist/coco-ssd.min.js"
);

let model = null;
let busy = false;

self.onmessage = async (e) => {
  const {type, payload} = e.data;
  if (type === "init") {
    try {
      model = await cocoSsd.load({base: "lite_mobilenet_v2"});
      self.postMessage({type: "ready"});
    } catch (err) {
      self.postMessage({type: "error", error: String(err)});
    }
  } else if (type === "detect") {
    if (!model || busy) return;
    busy = true;
    try {
      // payload is an ImageBitmap transferred from main thread
      const preds = await model.detect(payload, 20);
      self.postMessage({type: "result", predictions: preds.filter(p => p.class === "person")});
    } catch (err) {
      self.postMessage({type: "error", error: String(err)});
    } finally {
      busy = false;
      if (payload && payload.close) payload.close();
    }
  }
};
