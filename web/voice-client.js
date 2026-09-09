/**
 * Voice Agent Service - Browser WebRTC Client SDK (Task 1.7)
 *
 * Lightweight, zero-dependency (other than livekit-client) SDK for embedding
 * real-time voice agent capabilities into any browser, web application, or widget.
 *
 * Features:
 * - Clean EventEmitter subscription model for all voice agent events
 * - Automatic microphone management with Echo Cancellation (AEC) & Noise Suppression
 * - Audio visualizer and real-time volume analysis (0.0 to 1.0)
 * - Resilient connection lifecycle with exponential backoff reconnect logic
 * - Typed data channel packet handling: STT transcripts, VAD, LLM clauses, TTS metrics, mid-call tools, barge-in
 * - Bidirectional client actions: test prompts, tool triggering, speech testing, history clearing
 */

(function (root, factory) {
  if (typeof define === "function" && define.amd) {
    define(["livekit-client"], factory);
  } else if (typeof module === "object" && module.exports) {
    var lk = null;
    try { lk = require("livekit-client"); } catch (e) {}
    module.exports = factory(lk);
  } else {
    var lk = root.LivekitClient || root.LiveKitClient || root.livekit;
    root.VoiceAgentClient = factory(lk);
  }
})(typeof self !== "undefined" ? self : this, function (LiveKitClient) {
  "use strict";

  /**
   * Minimal lightweight EventEmitter
   */
  function EventEmitter() {
    this._events = {};
  }

  EventEmitter.prototype.on = function (event, listener) {
    if (typeof listener !== "function") throw new TypeError("Listener must be a function");
    if (!this._events[event]) this._events[event] = [];
    this._events[event].push(listener);
    return this;
  };

  EventEmitter.prototype.once = function (event, listener) {
    var self = this;
    function g() {
      self.off(event, g);
      listener.apply(this, arguments);
    }
    g.listener = listener;
    return this.on(event, g);
  };

  EventEmitter.prototype.off = function (event, listener) {
    if (!this._events[event]) return this;
    if (!listener) {
      delete this._events[event];
      return this;
    }
    this._events[event] = this._events[event].filter(function (fn) {
      return fn !== listener && fn.listener !== listener;
    });
    return this;
  };

  EventEmitter.prototype.emit = function (event) {
    var listeners = this._events[event];
    if (!listeners || !listeners.length) return false;
    var args = Array.prototype.slice.call(arguments, 1);
    var list = listeners.slice();
    for (var i = 0; i < list.length; i++) {
      try {
        list[i].apply(this, args);
      } catch (err) {
        console.error("Error in event listener for '" + event + "':", err);
      }
    }
    return true;
  };

  /**
   * Client Connection States
   */
  var ConnectionState = {
    DISCONNECTED: "disconnected",
    CONNECTING: "connecting",
    CONNECTED: "connected",
    RECONNECTING: "reconnecting",
    FAILED: "failed",
  };

  /**
   * AudioVisualizer Helper
   */
  function AudioVisualizer(options) {
    options = options || {};
    this.canvas = options.canvas || null;
    this.ctx = this.canvas ? this.canvas.getContext("2d") : null;
    this.type = options.type || "wave"; // 'wave', 'bars', 'meter'
    this.color = options.color || "#2563eb";
    this.barWidth = options.barWidth || 4;
    this.barGap = options.barGap || 2;
    this.analyser = options.analyser || null;
    this._rafId = null;
    this._running = false;
  }

  AudioVisualizer.prototype.setAnalyser = function (analyser) {
    this.analyser = analyser;
    if (this._running) {
      this.stop();
      this.start();
    }
  };

  AudioVisualizer.prototype.start = function (analyser) {
    if (analyser) this.analyser = analyser;
    if (!this.analyser || !this.ctx) return;
    this._running = true;
    var self = this;
    var bufferLength = self.analyser.frequencyBinCount;
    var dataArray = new Uint8Array(bufferLength);

    function draw() {
      if (!self._running) return;
      self._rafId = requestAnimationFrame(draw);

      var width = self.canvas.width;
      var height = self.canvas.height;
      self.ctx.clearRect(0, 0, width, height);

      if (self.type === "wave") {
        self.analyser.getByteTimeDomainData(dataArray);
        self.ctx.lineWidth = 2;
        self.ctx.strokeStyle = self.color;
        self.ctx.beginPath();

        var sliceWidth = width / bufferLength;
        var x = 0;
        for (var i = 0; i < bufferLength; i++) {
          var v = dataArray[i] / 128.0;
          var y = (v * height) / 2;
          if (i === 0) self.ctx.moveTo(x, y);
          else self.ctx.lineTo(x, y);
          x += sliceWidth;
        }
        self.ctx.lineTo(width, height / 2);
        self.ctx.stroke();
      } else if (self.type === "bars") {
        self.analyser.getByteFrequencyData(dataArray);
        var totalBars = Math.floor(width / (self.barWidth + self.barGap));
        var step = Math.floor(bufferLength / totalBars) || 1;
        self.ctx.fillStyle = self.color;

        for (var b = 0; b < totalBars; b++) {
          var val = dataArray[b * step] || 0;
          var barHeight = (val / 255) * height;
          var bx = b * (self.barWidth + self.barGap);
          var by = height - barHeight;
          self.ctx.fillRect(bx, by, self.barWidth, barHeight);
        }
      }
    }

    draw();
  };

  AudioVisualizer.prototype.stop = function () {
    this._running = false;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    if (this.ctx && this.canvas) {
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    }
  };

  /**
   * Main VoiceAgentClient class
   */
  function VoiceAgentClient(config) {
    EventEmitter.call(this);

    this.config = Object.assign(
      {
        url: null,
        token: null,
        tokenEndpoint: "/token",
        room: "test-room",
        identity: "caller-" + Math.random().toString(36).substring(2, 7),
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        reconnect: {
          enabled: true,
          maxRetries: 5,
          initialDelayMs: 1000,
          maxDelayMs: 10000,
          backoffMultiplier: 1.5,
        },
        autoSubscribeAudio: true,
      },
      config || {}
    );

    this.state = ConnectionState.DISCONNECTED;
    this.room = null;
    this.audioContext = null;
    this.localAnalyser = null;
    this.remoteAnalyser = null;
    this.isMuted = false;
    this.activeStreamingReply = "";
    this._retryCount = 0;
    this._reconnectTimer = null;
    this._meterRafId = null;
    this._remoteAudioElement = null;
    this._explicitDisconnect = false;
  }

  // Inherit EventEmitter
  VoiceAgentClient.prototype = Object.create(EventEmitter.prototype);
  VoiceAgentClient.prototype.constructor = VoiceAgentClient;

  VoiceAgentClient.ConnectionState = ConnectionState;
  VoiceAgentClient.AudioVisualizer = AudioVisualizer;

  /**
   * Update internal state and emit stateChange event
   */
  VoiceAgentClient.prototype._setState = function (newState, error) {
    var oldState = this.state;
    if (oldState === newState) return;
    this.state = newState;
    this.emit("stateChange", { state: newState, oldState: oldState, error: error });
    this.emit(newState, { oldState: oldState, error: error });
  };

  /**
   * Fetch access token from endpoint or configuration
   */
  VoiceAgentClient.prototype._resolveToken = async function () {
    if (typeof this.config.tokenProvider === "function") {
      var res = await this.config.tokenProvider({
        room: this.config.room,
        identity: this.config.identity,
      });
      if (typeof res === "string") return { token: res, url: this.config.url };
      return res; // { token, url }
    }

    if (this.config.token && this.config.url) {
      return { token: this.config.token, url: this.config.url };
    }

    // Default to token endpoint
    var ep = this.config.tokenEndpoint;
    var u = ep + (ep.indexOf("?") > -1 ? "&" : "?") +
      "room=" + encodeURIComponent(this.config.room) +
      "&user=" + encodeURIComponent(this.config.identity);

    var fetchRes = await fetch(u);
    if (!fetchRes.ok) throw new Error("Failed to fetch token: HTTP " + fetchRes.status);
    var data = await fetchRes.json();
    return { token: data.token, url: data.url || this.config.url };
  };

  /**
   * Connect to Voice Agent room
   */
  VoiceAgentClient.prototype.connect = async function (options) {
    if (this.state === ConnectionState.CONNECTED || this.state === ConnectionState.CONNECTING) {
      return this;
    }

    if (options) {
      if (options.room) this.config.room = options.room;
      if (options.identity) this.config.identity = options.identity;
      if (options.token) this.config.token = options.token;
      if (options.url) this.config.url = options.url;
    }

    this._explicitDisconnect = false;
    this._setState(ConnectionState.CONNECTING);

    var LK = LiveKitClient || (typeof window !== "undefined" ? (window.LivekitClient || window.LiveKitClient || window.livekit) : null);
    if (!LK) {
      var err = new Error("LiveKitClient library not found. Include livekit-client script or pass it in constructor.");
      this._setState(ConnectionState.FAILED, err);
      throw err;
    }

    try {
      var creds = await this._resolveToken();
      var wsUrl = creds.url;
      var jwt = creds.token;

      var room = new LK.Room({
        adaptiveStream: true,
        dynacast: true,
        audioCaptureDefaults: this.config.audio,
      });
      this.room = room;

      var self = this;

      // Room lifecycle events
      room.on(LK.RoomEvent.Connected, function () {
        self._retryCount = 0;
        self._setState(ConnectionState.CONNECTED);
        self.emit("roomConnected", { roomName: room.name, localParticipant: room.localParticipant });
      });

      room.on(LK.RoomEvent.Disconnected, function () {
        self._cleanupAudio();
        if (!self._explicitDisconnect && self.config.reconnect.enabled) {
          self._handleReconnect();
        } else {
          self._setState(ConnectionState.DISCONNECTED);
        }
      });

      room.on(LK.RoomEvent.Reconnecting, function () {
        self._setState(ConnectionState.RECONNECTING);
      });

      room.on(LK.RoomEvent.Reconnected, function () {
        self._setState(ConnectionState.CONNECTED);
        self.emit("reconnected");
      });

      // Participant events
      room.on(LK.RoomEvent.ParticipantConnected, function (p) {
        self.emit("participantConnected", p);
      });

      room.on(LK.RoomEvent.ParticipantDisconnected, function (p) {
        self.emit("participantDisconnected", p);
      });

      room.on(LK.RoomEvent.ActiveSpeakersChanged, function (speakers) {
        self.emit("activeSpeakersChanged", speakers);
      });

      // Track subscribed (agent audio playback)
      room.on(LK.RoomEvent.TrackSubscribed, function (track, pub, participant) {
        if (track.kind === "audio") {
          if (self.config.autoSubscribeAudio) {
            var el = track.attach();
            self._remoteAudioElement = el;
            document.body.appendChild(el);
          }
          self._setupRemoteAudioAnalysis(track);
          self.emit("agentTrackSubscribed", { track: track, participant: participant });
        }
      });

      // Data messages from the Voice Agent worker
      room.on(LK.RoomEvent.DataReceived, function (payload, participant, kind, topic) {
        var msg;
        try {
          msg = JSON.parse(new TextDecoder().decode(payload));
        } catch (e) {
          return;
        }

        self.emit("data", { topic: topic, message: msg, participant: participant });

        if (topic === "transcript") {
          self.emit("transcript", {
            text: msg.text,
            isFinal: !!msg.is_final,
            speaker: msg.speaker || "caller",
            timestamp: msg.timestamp || Date.now() / 1000,
          });
        } else if (topic === "vad") {
          self.emit("vad", {
            state: msg.state, // 'speaking' | 'listening' | 'away'
            oldState: msg.old_state,
            timestamp: msg.timestamp,
          });
        } else if (topic === "agent_state") {
          self.emit("agentState", {
            state: msg.state, // 'idle' | 'listening' | 'thinking' | 'speaking'
            oldState: msg.old_state,
            timestamp: msg.timestamp,
          });
        } else if (topic === "llm_stream") {
          self.activeStreamingReply += msg.token;
          self.emit("llmStream", {
            token: msg.token,
            text: self.activeStreamingReply,
            timestamp: msg.timestamp,
          });
        } else if (topic === "llm_clause") {
          self.emit("llmClause", {
            clause: msg.clause,
            isFinal: msg.is_final,
            index: msg.index,
            timestamp: msg.timestamp,
          });
        } else if (topic === "agent_reply") {
          self.activeStreamingReply = "";
          self.emit("agentReply", {
            text: msg.text,
            metrics: msg.metrics,
            timestamp: msg.timestamp,
          });
        } else if (topic === "tts_metrics") {
          self.emit("ttsMetrics", msg);
        } else if (topic === "interruption") {
          self.emit("interruption", {
            reason: msg.reason,
            timestamp: msg.timestamp,
          });
        } else if (topic === "tool_call") {
          self.emit("toolCall", {
            tool: msg.tool,
            arguments: msg.arguments,
            timestamp: msg.timestamp,
          });
        } else if (topic === "filler_speech") {
          self.emit("fillerSpeech", {
            phrase: msg.phrase,
            tool: msg.tool,
            timestamp: msg.timestamp,
          });
        } else if (topic === "tool_result") {
          self.emit("toolResult", {
            tool: msg.tool,
            status: msg.status,
            result: msg.result,
            durationMs: msg.duration_ms,
            error: msg.error,
            timestamp: msg.timestamp,
          });
        } else if (topic === "chat_history") {
          self.emit("chatHistory", msg.messages);
        }
      });

      // Connect WebRTC room
      await room.connect(wsUrl, jwt);

      // Publish local microphone
      try {
        await room.localParticipant.setMicrophoneEnabled(true);
        var pubs = Array.from(room.localParticipant.audioTrackPublications.values());
        if (pubs.length && pubs[0].track) {
          this._setupLocalAudioAnalysis(pubs[0].track);
        }
        this.emit("microphoneEnabled", true);
      } catch (micErr) {
        this.emit("microphoneError", micErr);
        console.warn("Microphone not available or permission denied:", micErr);
      }

      this._startAudioLevelMeter();

      return this;
    } catch (e) {
      this._cleanupAudio();
      this._setState(ConnectionState.FAILED, e);
      this.emit("error", e);
      throw e;
    }
  };

  /**
   * Disconnect from room
   */
  VoiceAgentClient.prototype.disconnect = async function () {
    this._explicitDisconnect = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._cleanupAudio();
    if (this.room) {
      await this.room.disconnect();
      this.room = null;
    }
    this._setState(ConnectionState.DISCONNECTED);
  };

  /**
   * Automatic Reconnection with Exponential Backoff
   */
  VoiceAgentClient.prototype._handleReconnect = function () {
    var self = this;
    var cfg = this.config.reconnect;
    if (this._retryCount >= cfg.maxRetries) {
      this._setState(ConnectionState.FAILED, new Error("Max reconnection attempts reached"));
      return;
    }

    this._setState(ConnectionState.RECONNECTING);
    this._retryCount++;

    var delay = Math.min(
      cfg.maxDelayMs,
      cfg.initialDelayMs * Math.pow(cfg.backoffMultiplier, this._retryCount - 1)
    );
    // Add 10% random jitter
    delay += Math.floor(Math.random() * (delay * 0.1));

    this.emit("reconnecting", { attempt: this._retryCount, delayMs: delay });

    this._reconnectTimer = setTimeout(function () {
      self.connect().catch(function (err) {
        console.warn("Reconnection attempt " + self._retryCount + " failed:", err);
      });
    }, delay);
  };

  /**
   * Microphone controls
   */
  VoiceAgentClient.prototype.mute = async function () {
    if (!this.room || !this.room.localParticipant) return;
    await this.room.localParticipant.setMicrophoneEnabled(false);
    this.isMuted = true;
    this.emit("muteChange", true);
  };

  VoiceAgentClient.prototype.unmute = async function () {
    if (!this.room || !this.room.localParticipant) return;
    await this.room.localParticipant.setMicrophoneEnabled(true);
    this.isMuted = false;
    this.emit("muteChange", false);
  };

  VoiceAgentClient.prototype.toggleMute = async function () {
    if (this.isMuted) await this.unmute();
    else await this.mute();
    return this.isMuted;
  };

  /**
   * Data channel action publishers
   */
  VoiceAgentClient.prototype.sendData = async function (action, payload, reliable) {
    if (!this.room || !this.room.localParticipant) {
      throw new Error("Client not connected");
    }
    var data = Object.assign({ action: action }, payload || {});
    var bytes = new TextEncoder().encode(JSON.stringify(data));
    await this.room.localParticipant.publishData(bytes, { reliable: reliable !== false });
  };

  VoiceAgentClient.prototype.sendPrompt = function (text) {
    return this.sendData("test_prompt", { text: text }, true);
  };

  VoiceAgentClient.prototype.callTool = function (toolName, args) {
    return this.sendData("call_tool", { tool: toolName, arguments: args || {} }, true);
  };

  VoiceAgentClient.prototype.testTts = function (text) {
    return this.sendData("test_tts", { text: text }, true);
  };

  VoiceAgentClient.prototype.testSpeech = function () {
    return this.sendData("test_speech", {}, true);
  };

  VoiceAgentClient.prototype.clearHistory = function () {
    return this.sendData("clear_history", {}, true);
  };

  /**
   * Audio analysis & level meter
   */
  VoiceAgentClient.prototype._ensureAudioContext = function () {
    if (!this.audioContext && typeof window !== "undefined") {
      var AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) this.audioContext = new AudioCtx();
    }
    return this.audioContext;
  };

  VoiceAgentClient.prototype._setupLocalAudioAnalysis = function (track) {
    try {
      var ctx = this._ensureAudioContext();
      if (!ctx || !track.mediaStreamTrack) return;
      var src = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
      var analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      this.localAnalyser = analyser;
    } catch (e) {
      console.warn("Could not set up local audio analyser:", e);
    }
  };

  VoiceAgentClient.prototype._setupRemoteAudioAnalysis = function (track) {
    try {
      var ctx = this._ensureAudioContext();
      if (!ctx || !track.mediaStreamTrack) return;
      var src = ctx.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
      var analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      this.remoteAnalyser = analyser;
    } catch (e) {
      console.warn("Could not set up remote audio analyser:", e);
    }
  };

  VoiceAgentClient.prototype._startAudioLevelMeter = function () {
    var self = this;
    if (typeof window === "undefined") return;

    var localBuf = new Uint8Array(128);
    var remoteBuf = new Uint8Array(128);

    function tick() {
      if (self.state !== ConnectionState.CONNECTED) return;
      self._meterRafId = requestAnimationFrame(tick);

      var localLevel = 0;
      var remoteLevel = 0;

      if (self.localAnalyser && !self.isMuted) {
        self.localAnalyser.getByteTimeDomainData(localBuf);
        var peak = 0;
        for (var i = 0; i < localBuf.length; i++) {
          peak = Math.max(peak, Math.abs(localBuf[i] - 128));
        }
        localLevel = Math.min(1.0, peak / 128.0);
      }

      if (self.remoteAnalyser) {
        self.remoteAnalyser.getByteTimeDomainData(remoteBuf);
        var rPeak = 0;
        for (var j = 0; j < remoteBuf.length; j++) {
          rPeak = Math.max(rPeak, Math.abs(remoteBuf[j] - 128));
        }
        remoteLevel = Math.min(1.0, rPeak / 128.0);
      }

      self.emit("audioLevel", { local: localLevel, remote: remoteLevel });
    }

    tick();
  };

  VoiceAgentClient.prototype._cleanupAudio = function () {
    if (this._meterRafId && typeof cancelAnimationFrame !== "undefined") {
      cancelAnimationFrame(this._meterRafId);
      this._meterRafId = null;
    }
    if (this.audioContext) {
      try { this.audioContext.close(); } catch (e) {}
      this.audioContext = null;
    }
    this.localAnalyser = null;
    this.remoteAnalyser = null;
    if (this._remoteAudioElement && this._remoteAudioElement.parentNode) {
      this._remoteAudioElement.parentNode.removeChild(this._remoteAudioElement);
      this._remoteAudioElement = null;
    }
  };

  /**
   * Helper factory to create a visualizer attached to this client's audio
   */
  VoiceAgentClient.prototype.createVisualizer = function (options) {
    options = options || {};
    var vis = new AudioVisualizer(options);
    var target = options.source === "agent" ? "remoteAnalyser" : "localAnalyser";
    if (this[target]) vis.setAnalyser(this[target]);

    var self = this;
    this.on(target === "remoteAnalyser" ? "agentTrackSubscribed" : "microphoneEnabled", function () {
      if (self[target]) vis.setAnalyser(self[target]);
    });

    return vis;
  };

  return VoiceAgentClient;
});
