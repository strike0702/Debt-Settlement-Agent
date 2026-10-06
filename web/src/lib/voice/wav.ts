/**
 * 16-bit mono PCM WAV encoding for the server STT path.
 *
 * vad-web hands `onSpeechEnd` a Float32Array at 16 kHz; the socket sends the
 * WAV as one binary frame (`app/voice/ws.py` `_on_wav`). Pure, no browser APIs.
 */

function writeAscii(view: DataView, offset: number, s: string): void {
  for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
}

/** Float samples in [-1, 1] → a complete RIFF/WAVE file (44-byte header + PCM). */
export function encodeWav(samples: Float32Array, sampleRate = 16000): ArrayBuffer {
  const pcmBytes = samples.length * 2;
  const buffer = new ArrayBuffer(44 + pcmBytes);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + pcmBytes, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeAscii(view, 36, "data");
  view.setUint32(40, pcmBytes, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]!));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}
