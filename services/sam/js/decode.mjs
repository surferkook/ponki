// Line-delimited JSON mask decoder for the finding service.
// In:  {"id": 1, "encoding": "one_bit", "payload": "!<base85>", "width": 60, "height": 27}
// Out: {"id": 1, "raster": "<base64 of width*height bytes of 0/1>"} or {"id": 1, "error": "..."}
// The payload keeps its wire marker (~ lossless, ! one_bit) in front.
import { createInterface } from "node:readline";
import { decodeMaskToRaster } from "@meta-sam/parser";

const MARKERS = { "~": "lossless", "!": "one_bit" };

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    return;
  }
  try {
    // The codec wants the payload with its wire marker (~ or !) still in front.
    let payload = String(req.payload || "");
    let encoding = req.encoding;
    if (payload && MARKERS[payload[0]]) {
      encoding = MARKERS[payload[0]];
    }
    const raster = decodeMaskToRaster({
      encoding,
      payload,
      width: req.width,
      height: req.height,
    });
    process.stdout.write(
      JSON.stringify({ id: req.id, raster: Buffer.from(raster).toString("base64") }) + "\n",
    );
  } catch (err) {
    process.stdout.write(
      JSON.stringify({ id: req.id, error: String(err && err.message ? err.message : err) }) + "\n",
    );
  }
});
