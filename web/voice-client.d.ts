/**
 * TypeScript definitions for VoiceAgentClient SDK (Task 1.7)
 */

export interface VoiceAgentConfig {
  url?: string;
  token?: string;
  tokenEndpoint?: string;
  tokenProvider?: (params: { room: string; identity: string }) => Promise<string | { token: string; url?: string }>;
  room?: string;
  identity?: string;
  audio?: {
    echoCancellation?: boolean;
    noiseSuppression?: boolean;
    autoGainControl?: boolean;
  };
  reconnect?: {
    enabled?: boolean;
    maxRetries?: number;
    initialDelayMs?: number;
    maxDelayMs?: number;
    backoffMultiplier?: number;
  };
  autoSubscribeAudio?: boolean;
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'failed';
export type AgentState = 'idle' | 'listening' | 'thinking' | 'speaking';
export type VadState = 'speaking' | 'listening' | 'away';

export interface TranscriptEvent {
  text: string;
  isFinal: boolean;
  speaker: string;
  timestamp: number;
}

export interface VadEvent {
  state: VadState;
  oldState?: string;
  timestamp?: number;
}

export interface AgentStateEvent {
  state: AgentState;
  oldState?: string;
  timestamp?: number;
}

export interface LlmStreamEvent {
  token: string;
  text: string;
  timestamp?: number;
}

export interface LlmClauseEvent {
  clause: string;
  isFinal: boolean;
  index: number;
  timestamp?: number;
}

export interface AgentReplyEvent {
  text: string;
  metrics?: {
    ttft_ms?: number;
    duration_ms?: number;
    token_count?: number;
    clause_count?: number;
  };
  timestamp?: number;
}

export interface TtsMetricsEvent {
  ttfa_ms?: number;
  duration_ms?: number;
  audio_duration_ms?: number;
  characters?: number;
  provider?: string;
  model?: string;
  interrupted?: boolean;
  timestamp?: number;
}

export interface ToolCallEvent {
  tool: string;
  arguments: Record<string, any>;
  timestamp?: number;
}

export interface FillerSpeechEvent {
  phrase: string;
  tool?: string;
  timestamp?: number;
}

export interface ToolResultEvent {
  tool: string;
  status: 'success' | 'fallback' | 'error';
  result?: any;
  durationMs?: number;
  error?: string;
  timestamp?: number;
}

export interface AudioLevelEvent {
  local: number;
  remote: number;
}

export interface VisualizerOptions {
  canvas?: HTMLCanvasElement;
  type?: 'wave' | 'bars';
  color?: string;
  barWidth?: number;
  barGap?: number;
  source?: 'local' | 'agent';
}

export declare class AudioVisualizer {
  constructor(options?: VisualizerOptions);
  canvas: HTMLCanvasElement | null;
  start(analyser?: AnalyserNode): void;
  stop(): void;
  setAnalyser(analyser: AnalyserNode): void;
}

export declare class VoiceAgentClient {
  constructor(config?: VoiceAgentConfig);

  static ConnectionState: Record<string, ConnectionState>;
  static AudioVisualizer: typeof AudioVisualizer;

  state: ConnectionState;
  room: any;
  isMuted: boolean;
  activeStreamingReply: string;

  connect(options?: { room?: string; identity?: string; token?: string; url?: string }): Promise<this>;
  disconnect(): Promise<void>;

  mute(): Promise<void>;
  unmute(): Promise<void>;
  toggleMute(): Promise<boolean>;

  sendPrompt(text: string): Promise<void>;
  callTool(toolName: string, args?: Record<string, any>): Promise<void>;
  testTts(text: string): Promise<void>;
  testSpeech(): Promise<void>;
  clearHistory(): Promise<void>;

  createVisualizer(options?: VisualizerOptions): AudioVisualizer;

  on(event: 'stateChange', listener: (e: { state: ConnectionState; oldState: ConnectionState; error?: Error }) => void): this;
  on(event: 'transcript', listener: (e: TranscriptEvent) => void): this;
  on(event: 'vad', listener: (e: VadEvent) => void): this;
  on(event: 'agentState', listener: (e: AgentStateEvent) => void): this;
  on(event: 'llmStream', listener: (e: LlmStreamEvent) => void): this;
  on(event: 'llmClause', listener: (e: LlmClauseEvent) => void): this;
  on(event: 'agentReply', listener: (e: AgentReplyEvent) => void): this;
  on(event: 'ttsMetrics', listener: (e: TtsMetricsEvent) => void): this;
  on(event: 'interruption', listener: (e: { reason: string; timestamp?: number }) => void): this;
  on(event: 'toolCall', listener: (e: ToolCallEvent) => void): this;
  on(event: 'fillerSpeech', listener: (e: FillerSpeechEvent) => void): this;
  on(event: 'toolResult', listener: (e: ToolResultEvent) => void): this;
  on(event: 'audioLevel', listener: (e: AudioLevelEvent) => void): this;
  on(event: 'error', listener: (err: Error) => void): this;
  on(event: string, listener: (...args: any[]) => void): this;

  off(event: string, listener?: (...args: any[]) => void): this;
  emit(event: string, ...args: any[]): boolean;
}

export default VoiceAgentClient;
