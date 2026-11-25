/**
 * Phonos User Model Worker SDK - Node.js
 * =======================================
 *
 * This SDK allows you to run custom ML models on your own infrastructure
 * and integrate them seamlessly with the Phonos platform.
 *
 * Installation:
 *     npm install axios
 *
 * Usage:
 *     1. Create a custom model in Phonos dashboard
 *     2. Generate an API key for your model
 *     3. Implement your model processing logic in the processJob() function
 *     4. Run this worker script
 *
 * Example:
 *     node worker.js --api-key="your-api-key-here" --endpoint="https://phonos.example.com"
 */

const axios = require('axios');
const fs = require('fs');
const path = require('path');

class PhonosWorker {
    /**
     * Initialize the Phonos Worker.
     *
     * @param {Object} config - Configuration object
     * @param {string} config.apiKey - Your Phonos model/project API key
     * @param {string} config.phonosEndpoint - Phonos server URL
     * @param {string} [config.workerId] - Optional worker identifier
     * @param {number} [config.pollInterval=5000] - Milliseconds between polls
     * @param {number} [config.maxRetries=3] - Max retries for failed requests
     */
    constructor({ apiKey, phonosEndpoint, workerId, pollInterval = 5000, maxRetries = 3 }) {
        this.apiKey = apiKey;
        this.phonosEndpoint = phonosEndpoint.replace(/\/$/, '');
        this.workerId = workerId || `worker-${process.pid}`;
        this.pollInterval = pollInterval;
        this.maxRetries = maxRetries;

        // Configure axios client
        this.client = axios.create({
            baseURL: this.phonosEndpoint,
            headers: {
                'Authorization': `Bearer ${apiKey}`,
                'Content-Type': 'application/json'
            },
            timeout: 30000
        });
    }

    /**
     * Log message with timestamp
     */
    log(level, message) {
        const timestamp = new Date().toISOString();
        console.log(`[${timestamp}] [${level.toUpperCase()}] ${message}`);
    }

    /**
     * Check if the Phonos Bridge API is accessible.
     */
    async healthCheck() {
        try {
            const response = await this.client.get('/api/bridge/v1/health');
            if (response.status === 200) {
                this.log('info', '✓ Bridge API is healthy');
                return true;
            }
            this.log('error', `Bridge API health check failed: ${response.status}`);
            return false;
        } catch (error) {
            this.log('error', `Bridge API health check error: ${error.message}`);
            return false;
        }
    }

    /**
     * Get information about this worker's authorized models and projects.
     */
    async getWorkerInfo() {
        try {
            const response = await this.client.get('/api/bridge/v1/worker/info');
            if (response.status === 200) {
                const info = response.data;
                this.log('info', `✓ Authorized for user ${info.user_id}`);
                this.log('info', `  Models: ${info.authorized_models?.length || 0}`);
                this.log('info', `  Projects: ${info.authorized_projects?.length || 0}`);
                return info;
            }
            return null;
        } catch (error) {
            if (error.response?.status === 401) {
                this.log('error', '✗ Invalid API key');
            } else {
                this.log('error', `Error getting worker info: ${error.message}`);
            }
            return null;
        }
    }

    /**
     * Poll for new jobs from Phonos.
     *
     * @param {Object} options - Poll options
     * @param {number} [options.modelId] - Specific model ID to poll for
     * @param {number} [options.maxJobs=1] - Maximum jobs to fetch
     * @returns {Promise<Array>} Array of job objects
     */
    async pollJobs({ modelId, maxJobs = 1 } = {}) {
        try {
            const payload = {
                worker_id: this.workerId,
                max_jobs: maxJobs
            };
            if (modelId) {
                payload.model_id = modelId;
            }

            const response = await this.client.post('/api/bridge/v1/jobs/poll', payload);

            if (response.status === 200) {
                const jobs = response.data.jobs || [];
                if (jobs.length > 0) {
                    this.log('info', `✓ Received ${jobs.length} job(s)`);
                }
                return jobs;
            }
            return [];
        } catch (error) {
            if (error.response?.status === 401) {
                this.log('error', '✗ Authentication failed - check your API key');
            } else {
                this.log('warn', `Poll request failed: ${error.message}`);
            }
            return [];
        }
    }

    /**
     * Submit job result back to Phonos.
     *
     * @param {Object} params - Result parameters
     * @param {string} params.jobId - Job ID
     * @param {string} params.status - "completed" or "failed"
     * @param {Object} params.result - Result data object
     * @param {Buffer} [params.outputFile] - Optional binary file output
     * @param {string} [params.errorMessage] - Optional error message
     * @param {Object} [params.metrics] - Optional metrics object
     * @returns {Promise<boolean>} True if successful
     */
    async submitResult({ jobId, status, result, outputFile, errorMessage, metrics = {} }) {
        try {
            const payload = {
                status,
                result,
                metrics
            };

            if (errorMessage) {
                payload.error_message = errorMessage;
            }

            if (outputFile) {
                payload.output_file = outputFile.toString('base64');
            }

            const response = await this.client.post(
                `/api/bridge/v1/jobs/${jobId}/result`,
                payload
            );

            if (response.status === 200) {
                this.log('info', `✓ Result submitted for job ${jobId}`);
                return true;
            }
            return false;
        } catch (error) {
            if (error.response?.status === 403) {
                this.log('error', `✗ Not authorized to submit results for job ${jobId}`);
            } else {
                this.log('error', `Failed to submit result: ${error.message}`);
            }
            return false;
        }
    }

    /**
     * Process a job. Override this method with your custom model logic.
     *
     * @param {Object} job - Job object
     * @param {string} job.job_id - Unique job identifier
     * @param {number} job.model_id - Model ID
     * @param {Object} job.input_data - Input data
     * @param {Object} job.custom_params - Custom parameters
     * @param {string} job.output_format - Expected output format
     * @returns {Promise<Object>} Result object
     */
    async processJob(job) {
        // IMPLEMENT YOUR MODEL LOGIC HERE
        this.log('info', `Processing job ${job.job_id} with model ${job.model_id}`);

        const { input_data, custom_params } = job;

        try {
            // TODO: Replace this with your actual model inference code
            // Example:
            // 1. Load your model
            // const model = await loadMyModel();
            //
            // 2. Process input
            // const result = await model.predict(input_data);
            //
            // 3. Generate output file if needed
            // const outputFile = await generateOutputFile(result);

            // Placeholder result
            const result = {
                message: 'Job processed successfully',
                input_received: input_data,
                params_received: custom_params
            };

            return {
                status: 'completed',
                result,
                outputFile: null,  // Set to Buffer if you have file output
                metrics: {
                    runtime_seconds: 1.0,
                    memory_used_mb: 128
                }
            };

        } catch (error) {
            this.log('error', `Error processing job: ${error.message}`);
            return {
                status: 'failed',
                result: {},
                errorMessage: error.message
            };
        }
    }

    /**
     * Start the worker main loop.
     *
     * @param {Object} options - Run options
     * @param {number} [options.modelId] - Specific model ID to process
     */
    async run({ modelId } = {}) {
        this.log('info', `Starting Phonos Worker ${this.workerId}`);
        this.log('info', `Endpoint: ${this.phonosEndpoint}`);

        // Health check
        const healthy = await this.healthCheck();
        if (!healthy) {
            this.log('error', 'Bridge API is not accessible. Exiting.');
            process.exit(1);
        }

        // Get worker info
        const info = await this.getWorkerInfo();
        if (!info) {
            this.log('error', 'Failed to authenticate. Check your API key.');
            process.exit(1);
        }

        this.log('info', `Worker started successfully. Polling every ${this.pollInterval}ms...`);
        this.log('info', 'Press Ctrl+C to stop');

        // Main loop
        const mainLoop = async () => {
            try {
                // Poll for jobs
                const jobs = await this.pollJobs({ modelId, maxJobs: 1 });

                // Process each job
                for (const job of jobs) {
                    const jobId = job.job_id;
                    this.log('info', `→ Processing job ${jobId}`);

                    const startTime = Date.now();
                    const result = await this.processJob(job);
                    const elapsed = (Date.now() - startTime) / 1000;

                    // Add runtime to metrics if not present
                    if (!result.metrics) {
                        result.metrics = {};
                    }
                    if (!result.metrics.runtime_seconds) {
                        result.metrics.runtime_seconds = elapsed;
                    }

                    // Submit result
                    const success = await this.submitResult({
                        jobId,
                        status: result.status,
                        result: result.result || {},
                        outputFile: result.outputFile,
                        errorMessage: result.errorMessage,
                        metrics: result.metrics
                    });

                    if (success) {
                        this.log('info', `✓ Job ${jobId} completed in ${elapsed.toFixed(2)}s`);
                    } else {
                        this.log('error', `✗ Failed to submit result for job ${jobId}`);
                    }
                }

                // Schedule next poll
                setTimeout(mainLoop, this.pollInterval);

            } catch (error) {
                this.log('error', `Worker error: ${error.message}`);
                // Retry after interval
                setTimeout(mainLoop, this.pollInterval);
            }
        };

        // Handle graceful shutdown
        process.on('SIGINT', () => {
            this.log('info', '\nWorker stopped by user');
            process.exit(0);
        });

        // Start main loop
        mainLoop();
    }
}

// Example usage
if (require.main === module) {
    const args = process.argv.slice(2);
    const config = {};

    // Parse command line arguments
    for (let i = 0; i < args.length; i++) {
        if (args[i].startsWith('--')) {
            const [key, value] = args[i].substring(2).split('=');
            config[key.replace(/-([a-z])/g, (g) => g[1].toUpperCase())] = value;
        }
    }

    if (!config.apiKey || !config.endpoint) {
        console.error('Usage: node worker.js --api-key=<key> --endpoint=<url> [--model-id=<id>] [--worker-id=<id>]');
        process.exit(1);
    }

    // Create and run worker
    const worker = new PhonosWorker({
        apiKey: config.apiKey,
        phonosEndpoint: config.endpoint,
        workerId: config.workerId,
        pollInterval: parseInt(config.pollInterval) || 5000
    });

    worker.run({ modelId: parseInt(config.modelId) });
}

module.exports = PhonosWorker;
