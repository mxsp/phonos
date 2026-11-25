/**
 * Phonos IoT Bridge - JavaScript SDK
 *
 * A JavaScript client for connecting devices to the Phonos IoT Bridge.
 * Works in Node.js and browsers.
 *
 * Node.js Installation:
 *   npm install node-fetch crypto-js
 *
 * Browser Usage:
 *   <script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js"></script>
 *   <script src="iot_sdk_javascript.js"></script>
 *
 * Example:
 *   const device = new PhonosDevice({
 *     apiKey: 'dk_your_device_api_key',
 *     serverUrl: 'https://your-phonos-server.com'
 *   });
 *
 *   await device.sendData({ temperature: 23.5 });
 */

(function(root, factory) {
    if (typeof module !== 'undefined' && module.exports) {
        // Node.js
        module.exports = factory(require);
    } else {
        // Browser
        root.PhonosDevice = factory(null);
        root.registerDevice = factory(null).registerDevice;
    }
})(typeof self !== 'undefined' ? self : this, function(nodeRequire) {
    'use strict';

    // Node.js compatibility
    let fetch, CryptoJS;
    if (typeof window === 'undefined') {
        fetch = nodeRequire('node-fetch');
        CryptoJS = nodeRequire('crypto-js');
    } else {
        fetch = window.fetch.bind(window);
        CryptoJS = window.CryptoJS;
    }

    /**
     * Phonos IoT Device Client
     */
    class PhonosDevice {
        /**
         * Create a new Phonos device client.
         *
         * @param {Object} options Configuration options
         * @param {string} options.apiKey Device API key (starts with 'dk_')
         * @param {string} options.serverUrl Phonos server URL
         * @param {boolean} options.encryption Enable encryption (default: true)
         * @param {boolean} options.autoHeartbeat Auto-send heartbeats (default: true)
         * @param {number} options.heartbeatInterval Heartbeat interval in ms (default: 60000)
         */
        constructor(options) {
            this.apiKey = options.apiKey;
            this.serverUrl = (options.serverUrl || 'https://phonos.io').replace(/\/$/, '');
            this.encryption = options.encryption !== false;
            this.baseUrl = `${this.serverUrl}/api/iot/v2`;

            this._commandHandlers = {};
            this._heartbeatTimer = null;
            this._running = false;

            if (options.autoHeartbeat !== false) {
                this.startHeartbeat(options.heartbeatInterval || 60000);
            }
        }

        /**
         * Generate authentication headers with signature.
         */
        _getHeaders(payload = '') {
            const timestamp = Math.floor(Date.now() / 1000);
            const signature = this._sign(timestamp, payload);

            return {
                'X-Device-Key': this.apiKey,
                'X-Timestamp': String(timestamp),
                'X-Signature': signature,
                'Content-Type': 'application/json'
            };
        }

        /**
         * Create HMAC-SHA256 signature.
         */
        _sign(timestamp, payload) {
            const message = `${timestamp}.${payload}`;
            return CryptoJS.HmacSHA256(message, this.apiKey).toString(CryptoJS.enc.Hex);
        }

        /**
         * Encrypt data using the API key.
         */
        _encrypt(data) {
            const plaintext = JSON.stringify(data);
            const keyBytes = CryptoJS.SHA256(this.apiKey);

            // XOR encryption
            const plaintextBytes = CryptoJS.enc.Utf8.parse(plaintext);
            const keyStream = this._expandKey(keyBytes, plaintextBytes.sigBytes);
            const ciphertext = this._xor(plaintextBytes, keyStream);

            // HMAC for integrity
            const mac = CryptoJS.HmacSHA256(ciphertext, keyBytes);

            // Pack: version(1) + mac(32) + ciphertext
            const version = CryptoJS.lib.WordArray.create([0], 1);
            const packed = version.concat(mac).concat(ciphertext);

            return CryptoJS.enc.Base64.stringify(packed);
        }

        /**
         * Decrypt data using the API key.
         */
        _decrypt(encrypted) {
            const packed = CryptoJS.enc.Base64.parse(encrypted);
            const bytes = this._wordArrayToBytes(packed);

            const version = bytes[0];
            if (version !== 0) {
                throw new Error(`Unsupported encryption version: ${version}`);
            }

            const mac = CryptoJS.lib.WordArray.create(bytes.slice(1, 33));
            const ciphertext = CryptoJS.lib.WordArray.create(bytes.slice(33));

            const keyBytes = CryptoJS.SHA256(this.apiKey);
            const expectedMac = CryptoJS.HmacSHA256(ciphertext, keyBytes);

            if (mac.toString() !== expectedMac.toString()) {
                throw new Error('HMAC verification failed');
            }

            const keyStream = this._expandKey(keyBytes, ciphertext.sigBytes);
            const plaintext = this._xor(ciphertext, keyStream);

            return JSON.parse(CryptoJS.enc.Utf8.stringify(plaintext));
        }

        _expandKey(key, length) {
            const keyBytes = this._wordArrayToBytes(key);
            const expanded = [];
            while (expanded.length < length) {
                expanded.push(...keyBytes);
            }
            return CryptoJS.lib.WordArray.create(expanded.slice(0, length));
        }

        _xor(a, b) {
            const aBytes = this._wordArrayToBytes(a);
            const bBytes = this._wordArrayToBytes(b);
            const result = aBytes.map((byte, i) => byte ^ bBytes[i]);
            return CryptoJS.lib.WordArray.create(result);
        }

        _wordArrayToBytes(wordArray) {
            const bytes = [];
            for (let i = 0; i < wordArray.sigBytes; i++) {
                bytes.push((wordArray.words[i >>> 2] >>> (24 - (i % 4) * 8)) & 0xff);
            }
            return bytes;
        }

        /**
         * Send data to the server.
         *
         * @param {Object} data Sensor readings or status data
         * @param {string} dataType Type of data ('reading', 'event', 'status', 'error')
         * @param {string} source Optional source identifier
         * @returns {Promise<Object>} Server response
         *
         * @example
         * await device.sendData({
         *   temperature: 23.5,
         *   humidity: 45.2
         * });
         */
        async sendData(data, dataType = 'reading', source = null) {
            const body = {
                type: dataType,
                device_timestamp: Math.floor(Date.now() / 1000)
            };

            if (source) {
                body.source = source;
            }

            if (this.encryption) {
                body.encrypted = true;
                body.data = this._encrypt(data);
            } else {
                body.data = data;
            }

            const payload = JSON.stringify(body);
            const headers = this._getHeaders(payload);

            const response = await fetch(`${this.baseUrl}/device/data`, {
                method: 'POST',
                headers,
                body: payload
            });

            const result = await response.json();

            // Handle commands returned with response
            if (result.commands) {
                let commands = result.commands;
                if (commands.encrypted) {
                    const decrypted = this._decrypt(commands.data);
                    commands = decrypted.commands || decrypted;
                }
                await this._processCommands(commands);
            }

            return result;
        }

        /**
         * Get pending commands from server.
         *
         * @returns {Promise<Array>} List of pending commands
         */
        async getCommands() {
            const headers = this._getHeaders();

            const response = await fetch(`${this.baseUrl}/device/commands`, {
                method: 'GET',
                headers
            });

            let result = await response.json();

            if (result.encrypted) {
                result = this._decrypt(result.data);
            }

            return result.commands || [];
        }

        /**
         * Acknowledge command execution.
         *
         * @param {string} commandId Command ID to acknowledge
         * @param {boolean} success Whether command succeeded
         * @param {Object} response Optional response data
         * @param {string} error Optional error message
         */
        async ackCommand(commandId, success = true, response = null, error = null) {
            const body = {
                command_id: commandId,
                success
            };

            if (response) body.response = response;
            if (error) body.error = error;

            const payload = JSON.stringify(body);
            const headers = this._getHeaders(payload);

            const res = await fetch(`${this.baseUrl}/device/commands/ack`, {
                method: 'POST',
                headers,
                body: payload
            });

            return res.json();
        }

        /**
         * Send heartbeat to maintain online status.
         */
        async heartbeat() {
            const headers = this._getHeaders();

            const response = await fetch(`${this.baseUrl}/device/heartbeat`, {
                method: 'POST',
                headers
            });

            return response.json();
        }

        /**
         * Register a command handler.
         *
         * @param {string} target Target/input name to handle
         * @param {Function} handler Callback function
         *
         * @example
         * device.onCommand('relay', async (payload) => {
         *   setRelay(payload.value);
         *   return { status: 'ok' };
         * });
         */
        onCommand(target, handler) {
            this._commandHandlers[target] = handler;
        }

        async _processCommands(commands) {
            for (const cmd of commands) {
                const handler = this._commandHandlers[cmd.target];
                if (handler) {
                    try {
                        const response = await handler(cmd.payload || {});
                        await this.ackCommand(cmd.command_id, true, response);
                    } catch (e) {
                        await this.ackCommand(cmd.command_id, false, null, e.message);
                    }
                }
            }
        }

        /**
         * Start automatic heartbeat.
         * @param {number} interval Interval in milliseconds
         */
        startHeartbeat(interval = 60000) {
            this.stopHeartbeat();
            this._heartbeatTimer = setInterval(() => {
                this.heartbeat().catch(() => {});
            }, interval);
        }

        /**
         * Stop automatic heartbeat.
         */
        stopHeartbeat() {
            if (this._heartbeatTimer) {
                clearInterval(this._heartbeatTimer);
                this._heartbeatTimer = null;
            }
        }

        /**
         * Run the device loop, polling for commands.
         * @param {number} pollInterval Poll interval in milliseconds
         */
        async run(pollInterval = 5000) {
            console.log(`Phonos IoT device running (poll every ${pollInterval}ms)...`);
            this._running = true;

            while (this._running) {
                try {
                    const commands = await this.getCommands();
                    await this._processCommands(commands);
                } catch (e) {
                    console.error('Error polling commands:', e);
                }
                await new Promise(r => setTimeout(r, pollInterval));
            }
        }

        /**
         * Stop the device loop.
         */
        stop() {
            this._running = false;
            this.stopHeartbeat();
        }
    }

    /**
     * Register a new device.
     *
     * @param {Object} options Registration options
     * @param {string} options.userApiKey Your Phonos user API key
     * @param {string} options.deviceId Unique device identifier
     * @param {string} options.deviceName Human-readable name
     * @param {string} options.serverUrl Phonos server URL
     * @param {string} options.deviceType Device type category
     * @param {Object} options.schema Optional I/O schema
     * @returns {Promise<Object>} Registration result with device_api_key
     *
     * @example
     * const result = await registerDevice({
     *   userApiKey: 'your_user_api_key',
     *   deviceId: 'lab-sensor-01',
     *   deviceName: 'Lab Sensor'
     * });
     * console.log('Device API Key:', result.device_api_key);
     */
    async function registerDevice(options) {
        const body = {
            user_api_key: options.userApiKey,
            device_id: options.deviceId,
            device_name: options.deviceName,
            device_type: options.deviceType || 'generic'
        };

        if (options.schema) {
            body.schema = options.schema;
        }

        const serverUrl = (options.serverUrl || 'https://phonos.io').replace(/\/$/, '');

        const response = await fetch(`${serverUrl}/api/iot/v2/device/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        return response.json();
    }

    // Export
    PhonosDevice.registerDevice = registerDevice;
    return PhonosDevice;
});


// =============================================================================
// EXAMPLE USAGE (Node.js)
// =============================================================================
/*
const PhonosDevice = require('./iot_sdk_javascript');

// Register a new device (one-time)
// const result = await PhonosDevice.registerDevice({
//     userApiKey: 'your_user_api_key',
//     deviceId: 'my-sensor-01',
//     deviceName: 'Living Room Sensor'
// });
// console.log('Save this API key:', result.device_api_key);

// Use existing device
const device = new PhonosDevice({
    apiKey: 'dk_your_device_api_key_here',
    serverUrl: 'https://phonos.io',
    encryption: true
});

// Register command handlers
device.onCommand('led', (payload) => {
    console.log('Setting LED to:', payload.value);
    // GPIO.write(LED_PIN, payload.value);
    return { status: 'ok', led: payload.value };
});

device.onCommand('config', (payload) => {
    console.log('Received config:', payload);
    return { status: 'configured' };
});

// Send data periodically
setInterval(async () => {
    await device.sendData({
        temperature: 20 + Math.random() * 10,
        humidity: 40 + Math.random() * 20
    });
}, 30000);

// Run command loop
device.run();
*/
