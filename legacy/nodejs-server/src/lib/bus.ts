import { EventEmitter } from 'node:events';

/** 进程内事件总线，用于 SSE 推送转写/纪要状态变化 */
export const bus = new EventEmitter();
bus.setMaxListeners(200);

export function emitRecordingEvent(recordingId: string, event: string, data: unknown) {
  bus.emit(`rec:${recordingId}`, { event, data, at: new Date().toISOString() });
}
