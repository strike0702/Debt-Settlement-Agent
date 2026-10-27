/**
 * Voice-activity detection: the pinned vad-web build and its tuned settings.
 *
 * vad-web and its ONNX runtime load from jsDelivr at mic-on time (not bundled):
 * `@ricky0123/vad-web@0.0.22` with `onnxruntime-web@1.14.0`, the pair that
 * works (1.18 broke it). The tuned settings are pinned by
 * `hooks/useVoice.test.ts`. The engine (`engine.ts`)
 * owns what happens on speech start/end; this file only builds the detector.
 */

export const VAD_WEB_VERSION = "0.0.22";
export const ONNX_RUNTIME_VERSION = "1.14.0";
const VAD_ESM = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_WEB_VERSION}/+esm`;
const VAD_ASSETS = `https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@${VAD_WEB_VERSION}/dist/`;
const ONNX_WASM = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ONNX_RUNTIME_VERSION}/dist/`;

/**
 * The detector's own settings: the sensitivity while the agent is silent, so
 * short answers ("No", "$35") still register. frameSamples = 1536 at 16 kHz
 * ≈ 96 ms per frame.
 */
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

/** Sensitivity while the agent is silent (the detector's own thresholds). */
export const LISTEN_SENSITIVITY = {
  positiveSpeechThreshold: VAD_OPTIONS.positiveSpeechThreshold,
  minSpeechFrames: VAD_OPTIONS.minSpeechFrames,
} as const;

/**
 * Evidence needed to barge while the agent is speaking. Chrome's echo
 * cancellation does not remove speechSynthesis output, so the agent's own
 * voice reaches the mic at moderate speech probability; a rep talking over it
 * scores higher and longer. The engine counts frames from `onFrameProcessed`:
 * a frame at or above `positiveSpeechThreshold` adds one, a frame below
 * `negativeSpeechThreshold` resets the run, frames in between hold it.
 * `minSpeechFrames` × 96 ms ≈ 480 ms of strong speech barges.
 */
export const BARGE_SENSITIVITY = {
  positiveSpeechThreshold: 0.8,
  negativeSpeechThreshold: 0.5,
  minSpeechFrames: 5,
} as const;

export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { channelCount: 1, echoCancellation: true, autoGainControl: true, noiseSuppression: true },
};

/** vad-web's per-frame speech probabilities. */
export interface SpeechProbabilities {
  isSpeech: number;
  notSpeech: number;
}

export interface VadCallbacks {
  onFrameProcessed: (probs: SpeechProbabilities) => void;
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
