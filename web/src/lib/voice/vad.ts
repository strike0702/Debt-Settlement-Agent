/**
 * Voice-activity detection: the pinned vad-web build and its Phase 21 settings.
 *
 * vad-web and its ONNX runtime load from jsDelivr at mic-on time (not bundled):
 * `@ricky0123/vad-web@0.0.22` with `onnxruntime-web@1.14.0`, the pair that
 * works (1.18 broke it, see PROGRESS "Accept/counter + mic release"). Tuning
 * is from PROGRESS "VAD sensitivity" and Phase 21. The engine (`engine.ts`)
 * owns what happens on speech start/end; this file only builds the detector.
 */

export const VAD_WEB_VERSION = "0.0.22";
export const ONNX_RUNTIME_VERSION = "1.14.0";
const VAD_ESM = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_WEB_VERSION}/+esm`;
const VAD_ASSETS = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_WEB_VERSION}/dist/`;
const ONNX_WASM = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ONNX_RUNTIME_VERSION}/dist/`;

/** frameSamples = 1536 at 16 kHz ≈ 96 ms per frame. */
export const VAD_OPTIONS = {
  // The 0.5 default is too deaf for quiet laptop mics.
  positiveSpeechThreshold: 0.35,
  negativeSpeechThreshold: 0.2,
  // ~770 ms of silence ends the utterance. A pause that splits one sentence is
  // safe: the server merges a fragment that lands during NLU into that turn.
  redemptionFrames: 8,
  // ~1 s of audio before the first speech frame, so word onsets are kept.
  preSpeechPadFrames: 10,
  // ~290 ms minimum; shorter is a misfire.
  minSpeechFrames: 3,
} as const;

export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { channelCount: 1, echoCancellation: true, autoGainControl: true, noiseSuppression: true },
};

export interface VadCallbacks {
  onSpeechStart: () => void;
  onVADMisfire: () => void;
  onSpeechEnd: (audio: Float32Array) => void;
}

/** The parts of vad-web's `MicVAD` the engine uses. */
export interface VadLike {
  start(): void;
  pause(): void;
  destroy(): void;
  audioContext?: { state: string; resume(): Promise<void> };
  stream?: MediaStream;
}

/** Build a MicVAD on `stream` (0.0.22 takes a ready `stream`, not `getStream`). */
export async function createMicVad(stream: MediaStream, callbacks: VadCallbacks): Promise<VadLike> {
  const mod = (await import(/* @vite-ignore */ VAD_ESM)) as {
    MicVAD: { new: (opts: Record<string, unknown>) => Promise<VadLike> };
  };
  return mod.MicVAD.new({
    stream,
    onnxWASMBasePath: ONNX_WASM,
    baseAssetPath: VAD_ASSETS,
    ...VAD_OPTIONS,
    ...callbacks,
  });
}
