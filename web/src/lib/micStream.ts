/**
 * 预热麦克风：首页点「录音」时立刻在用户手势里申请麦克风，
 * 拿到 MediaStream 后暂存，录音页挂载时直接接管。
 *
 * 这样做的原因：
 * 1. Safari（含 iOS）要求 getUserMedia 在用户手势调用栈内，路由跳转后就丢手势了；
 * 2. 提前拿到流，录音页可以「点一下立即开始」，不用再等一次权限弹窗/设备打开。
 */
let pending: MediaStream | null = null;
let timer: number | null = null;

/**
 * 录音统一使用的采集约束。
 * 16kHz 单声道对语音转写足够，比默认 48kHz 显著降低采集与编码负载；
 * Safari 不支持该约束时会自动忽略并回退到设备默认采样率。
 */
export const AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    sampleRate: 16000,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
  },
};

export function supportsMic(): boolean {
  return typeof navigator.mediaDevices?.getUserMedia === 'function' && window.isSecureContext;
}

/** 在点击事件里调用；失败静默，录音页会再走一次正常流程。 */
export function prewarmMic(keepMs = 15000): void {
  if (!supportsMic()) return;
  if (pending) return;
  navigator.mediaDevices
    .getUserMedia(AUDIO_CONSTRAINTS)
    .then((s) => {
      // 上一个还没被接管就丢掉旧的，避免轨道泄漏
      stop(pending);
      pending = s;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        stop(pending);
        pending = null;
        timer = null;
      }, keepMs);
    })
    .catch(() => {
      /* 用户拒绝或没有设备，交给录音页兜底提示 */
    });
}

/** 取走预热的流（一次性）。 */
export function takeMicStream(): MediaStream | null {
  const s = pending;
  pending = null;
  if (timer) window.clearTimeout(timer);
  timer = null;
  if (s && s.getAudioTracks().some((t) => t.readyState === 'live')) return s;
  stop(s);
  return null;
}

export function dropMicStream(s?: MediaStream | null): void {
  stop(s ?? pending);
  if (!s) pending = null;
}

function stop(s: MediaStream | null | undefined): void {
  s?.getTracks().forEach((t) => t.stop());
}
