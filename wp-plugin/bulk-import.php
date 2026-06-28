<?php
/**
 * Plugin Name: Bulk Product Importer
 * Description: Native WordPress dashboard for the Python ingestion engine.
 *              Runs engine.py detached in the background and displays live
 *              progress via a polled status.json file.
 * Version:     1.0.0
 * Author:      YusufQuresh1
 * Licence:     MIT
 *
 * Installation:
 *   Copy this file to /wp-content/plugins/bulk-import/bulk-import.php
 *   then activate it from WP Admin → Plugins.
 *
 * ─── CONFIGURE THE FOUR CONSTANTS BELOW BEFORE ACTIVATING ────────────────
 */

// Absolute path to python3 on the Bluehost server.
define( 'BULK_IMPORT_PYTHON',      '/usr/bin/python3' );

// Absolute path to engine.py on the server.
define( 'BULK_IMPORT_ENGINE',      '/home/username/ingestion/engine.py' );

// Absolute path to the folder containing the CSV + images/ subfolder.
define( 'BULK_IMPORT_CSV_DIR',     '/home/username/import' );

// Absolute path to the WordPress root (where wp-config.php lives).
define( 'BULK_IMPORT_WP_PATH',     '/home/username/public_html' );

// ── Runtime file paths (auto-derived, no changes needed below) ────────────
define( 'BULK_IMPORT_STATUS_FILE', sys_get_temp_dir() . '/bulk-import-status.json' );
define( 'BULK_IMPORT_PID_FILE',    sys_get_temp_dir() . '/bulk-import.pid' );
define( 'BULK_IMPORT_LOG_FILE',    sys_get_temp_dir() . '/bulk-import.log' );

// ─────────────────────────────────────────────────────────────────────────────

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

// ---------------------------------------------------------------------------
// Admin menu registration
// ---------------------------------------------------------------------------

add_action( 'admin_menu', 'bulk_import_register_menu' );

function bulk_import_register_menu() {
	add_menu_page(
		'Bulk Product Import',
		'Bulk Import',
		'manage_woocommerce',
		'bulk-import',
		'bulk_import_render_page',
		'dashicons-upload',
		58
	);
}

// ---------------------------------------------------------------------------
// Admin page HTML
// ---------------------------------------------------------------------------

function bulk_import_render_page() {
	if ( ! current_user_can( 'manage_woocommerce' ) ) {
		wp_die( 'Insufficient permissions.' );
	}

	$nonce        = wp_create_nonce( 'bulk_import_nonce' );
	$ajax_url     = admin_url( 'admin-ajax.php' );
	$is_running   = file_exists( BULK_IMPORT_PID_FILE );
	$status_label = $is_running ? 'Running' : 'Idle';
	?>
	<div class="wrap" id="bulk-import-wrap">
		<style>
			#bulk-import-wrap { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; max-width: 860px; }
			.bi-card { background: #0f0f0f; border-radius: 12px; padding: 28px 32px; margin-bottom: 20px; border: 1px solid #1e1e1e; }
			.bi-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
			.bi-title { font-size: 22px; font-weight: 800; color: #fff; letter-spacing: -0.5px; margin: 0; }
			.bi-badge { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;
				padding: 4px 10px; border-radius: 20px; }
			.bi-badge.idle    { background: #1e1e1e; color: #666; }
			.bi-badge.running { background: rgba(21,128,61,0.2); color: #4ade80; border: 1px solid rgba(74,222,128,0.3); }
			.bi-badge.complete{ background: rgba(21,128,61,0.3); color: #22c55e; }
			.bi-badge.error   { background: rgba(220,38,38,0.2); color: #f87171; }
			.bi-subtitle { color: #555; font-size: 13px; margin: 4px 0 24px; }

			/* Buttons */
			.bi-btn { display: inline-flex; align-items: center; gap: 8px; padding: 10px 22px;
				border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer;
				border: none; transition: all .2s; text-transform: uppercase; letter-spacing: .5px; }
			.bi-btn-start { background: #15803d; color: #fff; }
			.bi-btn-start:hover:not(:disabled) { background: #16a34a; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(21,128,61,.4); }
			.bi-btn-stop  { background: #1e1e1e; color: #ef4444; border: 1px solid #2a2a2a; margin-left: 10px; }
			.bi-btn-stop:hover:not(:disabled) { background: rgba(220,38,38,.1); border-color: #ef4444; }
			.bi-btn:disabled { opacity: .4; cursor: not-allowed; transform: none !important; }
			.bi-btn-icon { font-size: 15px; }

			/* Progress */
			.bi-progress-wrap { margin: 20px 0 10px; }
			.bi-progress-label { display: flex; justify-content: space-between; color: #888; font-size: 12px; margin-bottom: 8px; }
			.bi-progress-bar-bg { background: #1a1a1a; border-radius: 99px; height: 10px; overflow: hidden; }
			.bi-progress-bar { height: 100%; border-radius: 99px; background: linear-gradient(90deg,#15803d,#22c55e);
				transition: width .6s ease; width: 0%; }

			/* Stats row */
			.bi-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }
			.bi-stat { background: #141414; border-radius: 8px; padding: 14px 16px; border: 1px solid #1e1e1e; }
			.bi-stat-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #555; }
			.bi-stat-value { font-size: 26px; font-weight: 800; color: #fff; margin-top: 4px; }
			.bi-stat.imported .bi-stat-value { color: #22c55e; }
			.bi-stat.skipped  .bi-stat-value { color: #f59e0b; }
			.bi-stat.failed   .bi-stat-value { color: #ef4444; }

			/* Log window */
			.bi-log-title { color: #555; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
			.bi-log-window { background: #0a0a0a; border: 1px solid #1a1a1a; border-radius: 8px;
				padding: 14px 16px; font-family: "SF Mono","Fira Code",monospace; font-size: 12px;
				color: #4ade80; line-height: 1.8; min-height: 110px; }
			.bi-log-window .log-line { color: #6b7280; }
			.bi-log-window .log-line:last-child { color: #4ade80; }

			/* Config note */
			.bi-config-note { background: #111; border-left: 3px solid #15803d; border-radius: 0 8px 8px 0;
				padding: 14px 18px; color: #666; font-size: 12px; line-height: 1.7; }
			.bi-config-note code { background: #1a1a1a; padding: 2px 6px; border-radius: 4px; color: #a3e635; font-size: 11px; }
		</style>

		<div style="margin-bottom:20px;margin-top:12px;">
			<h1 style="color:#1d2327;font-size:26px;font-weight:800;margin:0 0 4px;">
				⚡ Bulk Product Import
			</h1>
			<p style="color:#666;margin:0;font-size:14px;">
				Background ingestion engine — streams 25k+ products from eBay CSV into WooCommerce.
				Prices are automatically reduced by 10% (<em>P<sub>new</sub> = P<sub>eBay</sub> × 0.9</em>).
			</p>
		</div>

		<!-- Control card -->
		<div class="bi-card">
			<div class="bi-header">
				<h2 class="bi-title">Ingestion Engine</h2>
				<span class="bi-badge <?php echo $is_running ? 'running' : 'idle'; ?>" id="bi-status-badge">
					<?php echo esc_html( $status_label ); ?>
				</span>
			</div>
			<p class="bi-subtitle">
				CSV directory: <code style="color:#a3e635;font-size:11px;"><?php echo esc_html( BULK_IMPORT_CSV_DIR ); ?></code>
			</p>

			<button class="bi-btn bi-btn-start" id="bi-start-btn"
				<?php echo $is_running ? 'disabled' : ''; ?>>
				<span class="bi-btn-icon">▶</span> Start Ingestion
			</button>
			<button class="bi-btn bi-btn-stop" id="bi-stop-btn"
				<?php echo ! $is_running ? 'disabled' : ''; ?>>
				<span class="bi-btn-icon">■</span> Stop / Cancel
			</button>

			<!-- Progress bar -->
			<div class="bi-progress-wrap">
				<div class="bi-progress-label">
					<span id="bi-progress-label">Ready</span>
					<span id="bi-progress-pct">0%</span>
				</div>
				<div class="bi-progress-bar-bg">
					<div class="bi-progress-bar" id="bi-progress-bar"></div>
				</div>
			</div>

			<!-- Stats -->
			<div class="bi-stats">
				<div class="bi-stat">
					<div class="bi-stat-label">Processed</div>
					<div class="bi-stat-value" id="bi-stat-processed">—</div>
				</div>
				<div class="bi-stat imported">
					<div class="bi-stat-label">✓ Imported</div>
					<div class="bi-stat-value" id="bi-stat-imported">—</div>
				</div>
				<div class="bi-stat skipped">
					<div class="bi-stat-label">⊘ Skipped</div>
					<div class="bi-stat-value" id="bi-stat-skipped">—</div>
				</div>
				<div class="bi-stat failed">
					<div class="bi-stat-label">✗ Failed</div>
					<div class="bi-stat-value" id="bi-stat-failed">—</div>
				</div>
			</div>

			<!-- Live log tail -->
			<div class="bi-log-title">Last 5 Operations</div>
			<div class="bi-log-window" id="bi-log-window">
				<span style="color:#2a2a2a;">Waiting for engine to start…</span>
			</div>
		</div>

		<!-- Config reminder card -->
		<div class="bi-config-note">
			<strong style="color:#a3e635;">📋 Before first use:</strong> open
			<code>bulk-import.php</code> and set the four constants at the top:
			<code>BULK_IMPORT_PYTHON</code>, <code>BULK_IMPORT_ENGINE</code>,
			<code>BULK_IMPORT_CSV_DIR</code>, and <code>BULK_IMPORT_WP_PATH</code>.
		</div>
	</div>

	<script>
	(function () {
		'use strict';

		const ajaxUrl  = <?php echo json_encode( $ajax_url ); ?>;
		const nonce    = <?php echo json_encode( $nonce ); ?>;
		let   pollTimer = null;

		const startBtn   = document.getElementById('bi-start-btn');
		const stopBtn    = document.getElementById('bi-stop-btn');
		const badge      = document.getElementById('bi-status-badge');
		const bar        = document.getElementById('bi-progress-bar');
		const barLabel   = document.getElementById('bi-progress-label');
		const barPct     = document.getElementById('bi-progress-pct');
		const logWindow  = document.getElementById('bi-log-window');
		const statProcessed = document.getElementById('bi-stat-processed');
		const statImported  = document.getElementById('bi-stat-imported');
		const statSkipped   = document.getElementById('bi-stat-skipped');
		const statFailed    = document.getElementById('bi-stat-failed');

		// ── Helpers ──────────────────────────────────────────────────────────

		function fmt(n) {
			return Number(n).toLocaleString('en-GB');
		}

		function setBadge(text, cls) {
			badge.textContent = text;
			badge.className   = 'bi-badge ' + cls;
		}

		function setButtons(running) {
			startBtn.disabled = running;
			stopBtn.disabled  = !running;
		}

		function renderStatus(data) {
			const pct = parseFloat(data.percent) || 0;
			bar.style.width = pct + '%';
			barPct.textContent = pct.toFixed(1) + '%';

			const total = parseInt(data.total) || 0;
			const proc  = parseInt(data.processed) || 0;
			barLabel.textContent = total
				? proc.toLocaleString('en-GB') + ' / ' + total.toLocaleString('en-GB') + ' rows'
				: 'Processing…';

			statProcessed.textContent = fmt(data.processed || 0);
			statImported.textContent  = fmt(data.imported  || 0);
			statSkipped.textContent   = fmt(data.skipped   || 0);
			statFailed.textContent    = fmt(data.failed    || 0);

			// Log tail — last 5 lines
			if (Array.isArray(data.recent_logs) && data.recent_logs.length) {
				logWindow.innerHTML = data.recent_logs
					.map(function (line) {
						return '<div class="log-line">' +
							line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
						'</div>';
					})
					.join('');
			}

			if (data.status === 'running') {
				setBadge('Running', 'running');
				setButtons(true);
				if (!pollTimer) startPolling();
			} else if (data.status === 'complete') {
				setBadge('Complete', 'complete');
				setButtons(false);
				bar.style.width = '100%';
				barPct.textContent = '100%';
				stopPolling();
				barLabel.textContent = 'Complete — elapsed ' + (data.elapsed || '');
			} else {
				setBadge('Idle', 'idle');
				setButtons(false);
				stopPolling();
			}
		}

		// ── Polling ──────────────────────────────────────────────────────────

		function poll() {
			fetch(ajaxUrl + '?action=bulk_import_status&_wpnonce=' + encodeURIComponent(nonce))
				.then(function (r) { return r.json(); })
				.then(function (data) {
					if (data.success && data.data) {
						renderStatus(data.data);
					}
				})
				.catch(function () { /* network blip — ignore, retry on next tick */ });
		}

		function startPolling() {
			if (pollTimer) return;
			pollTimer = setInterval(poll, 2500); // Poll every 2.5 s.
			poll(); // Immediate first call.
		}

		function stopPolling() {
			if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
		}

		// ── Start ─────────────────────────────────────────────────────────────

		startBtn.addEventListener('click', function () {
			startBtn.disabled = true;
			startBtn.innerHTML = '<span class="bi-btn-icon">⏳</span> Starting…';
			setBadge('Starting…', 'running');

			const body = new URLSearchParams({ action: 'bulk_import_start', _wpnonce: nonce });
			fetch(ajaxUrl, { method: 'POST', body: body })
				.then(function (r) { return r.json(); })
				.then(function (data) {
					if (data.success) {
						startBtn.innerHTML = '<span class="bi-btn-icon">▶</span> Start Ingestion';
						setButtons(true);
						startPolling();
					} else {
						setBadge('Error', 'error');
						alert('Failed to start: ' + (data.data || 'Unknown error'));
						startBtn.disabled = false;
						startBtn.innerHTML = '<span class="bi-btn-icon">▶</span> Start Ingestion';
					}
				})
				.catch(function (err) {
					setBadge('Error', 'error');
					alert('Network error: ' + err.message);
					startBtn.disabled = false;
					startBtn.innerHTML = '<span class="bi-btn-icon">▶</span> Start Ingestion';
				});
		});

		// ── Stop ──────────────────────────────────────────────────────────────

		stopBtn.addEventListener('click', function () {
			if (!confirm('Stop the ingestion engine? It can be safely restarted — already-imported products will be skipped.')) {
				return;
			}
			stopBtn.disabled = true;
			const body = new URLSearchParams({ action: 'bulk_import_stop', _wpnonce: nonce });
			fetch(ajaxUrl, { method: 'POST', body: body })
				.then(function (r) { return r.json(); })
				.then(function (data) {
					setBadge('Idle', 'idle');
					setButtons(false);
					stopPolling();
					barLabel.textContent = data.success ? 'Stopped by user.' : 'Stop attempted.';
				});
		});

		// ── Auto-start polling if already running ─────────────────────────────
		<?php if ( $is_running ) : ?>
		startPolling();
		<?php endif; ?>

	}());
	</script>
	<?php
}

// ---------------------------------------------------------------------------
// AJAX: Start ingestion
// ---------------------------------------------------------------------------

add_action( 'wp_ajax_bulk_import_start', 'bulk_import_ajax_start' );

function bulk_import_ajax_start() {
	check_ajax_referer( 'bulk_import_nonce' );

	if ( ! current_user_can( 'manage_woocommerce' ) ) {
		wp_send_json_error( 'Insufficient permissions.' );
	}

	// Safety check: refuse to spawn a second process.
	if ( file_exists( BULK_IMPORT_PID_FILE ) ) {
		$pid = (int) file_get_contents( BULK_IMPORT_PID_FILE );
		// Verify the PID is actually alive before rejecting.
		if ( $pid > 0 && posix_kill( $pid, 0 ) ) {
			wp_send_json_error( 'Engine is already running (PID ' . $pid . ').' );
		}
		// Stale PID file — clean up and allow restart.
		@unlink( BULK_IMPORT_PID_FILE );
	}

	// Clear any previous status file so the UI resets cleanly.
	if ( file_exists( BULK_IMPORT_STATUS_FILE ) ) {
		@unlink( BULK_IMPORT_STATUS_FILE );
	}

	// Build the detached command.
	// nohup + & ensures the process survives the PHP request.
	// 2>&1 redirects stderr to the log file so errors are captured.
	$python = escapeshellcmd( BULK_IMPORT_PYTHON );
	$engine = escapeshellarg( BULK_IMPORT_ENGINE );
	$csvdir = escapeshellarg( BULK_IMPORT_CSV_DIR );
	$wppath = escapeshellarg( BULK_IMPORT_WP_PATH );
	$status = escapeshellarg( BULK_IMPORT_STATUS_FILE );
	$pid_f  = escapeshellarg( BULK_IMPORT_PID_FILE );
	$log_f  = escapeshellarg( BULK_IMPORT_LOG_FILE );

	$cmd = sprintf(
		'nohup %s %s --csv-dir %s --wp-path %s --auto --status-file %s --pid-file %s --log-file %s > /dev/null 2>&1 &',
		$python, $engine, $csvdir, $wppath, $status, $pid_f, $log_f
	);

	// shell_exec runs the command; the trailing & detaches it immediately.
	// The PHP request completes and the user can navigate freely.
	shell_exec( $cmd );

	// Give the process 1 s to write its PID file, then confirm it started.
	sleep( 1 );

	if ( file_exists( BULK_IMPORT_PID_FILE ) ) {
		$pid = (int) file_get_contents( BULK_IMPORT_PID_FILE );
		wp_send_json_success( array( 'pid' => $pid ) );
	} else {
		// PID file not yet written — accept optimistically.
		// The JS poller will confirm within 2.5 s.
		wp_send_json_success( array( 'pid' => null, 'note' => 'Starting…' ) );
	}
}

// ---------------------------------------------------------------------------
// AJAX: Stop / cancel ingestion
// ---------------------------------------------------------------------------

add_action( 'wp_ajax_bulk_import_stop', 'bulk_import_ajax_stop' );

function bulk_import_ajax_stop() {
	check_ajax_referer( 'bulk_import_nonce' );

	if ( ! current_user_can( 'manage_woocommerce' ) ) {
		wp_send_json_error( 'Insufficient permissions.' );
	}

	if ( ! file_exists( BULK_IMPORT_PID_FILE ) ) {
		wp_send_json_success( array( 'message' => 'No running process found.' ) );
	}

	$pid = (int) file_get_contents( BULK_IMPORT_PID_FILE );

	if ( $pid > 0 ) {
		// SIGTERM: allow the process to flush its status.json before exiting.
		posix_kill( $pid, SIGTERM );
		sleep( 1 );
		// SIGKILL if still alive.
		if ( posix_kill( $pid, 0 ) ) {
			posix_kill( $pid, SIGKILL );
		}
	}

	@unlink( BULK_IMPORT_PID_FILE );

	// Update status.json so the UI shows "Idle" immediately.
	if ( file_exists( BULK_IMPORT_STATUS_FILE ) ) {
		$prev = json_decode( file_get_contents( BULK_IMPORT_STATUS_FILE ), true ) ?: array();
		$prev['status'] = 'idle';
		file_put_contents( BULK_IMPORT_STATUS_FILE, json_encode( $prev ) );
	}

	wp_send_json_success( array( 'message' => 'Process terminated.' ) );
}

// ---------------------------------------------------------------------------
// AJAX: Poll status
// ---------------------------------------------------------------------------

add_action( 'wp_ajax_bulk_import_status', 'bulk_import_ajax_status' );

function bulk_import_ajax_status() {
	check_ajax_referer( 'bulk_import_nonce' );

	if ( ! current_user_can( 'manage_woocommerce' ) ) {
		wp_send_json_error( 'Insufficient permissions.' );
	}

	if ( ! file_exists( BULK_IMPORT_STATUS_FILE ) ) {
		// No status file yet — engine may still be initialising.
		$is_running = file_exists( BULK_IMPORT_PID_FILE );
		wp_send_json_success( array(
			'status'      => $is_running ? 'running' : 'idle',
			'percent'     => 0,
			'processed'   => 0,
			'total'       => 0,
			'imported'    => 0,
			'skipped'     => 0,
			'failed'      => 0,
			'recent_logs' => array(),
			'elapsed'     => '00:00:00',
		) );
	}

	$raw = file_get_contents( BULK_IMPORT_STATUS_FILE );
	$data = json_decode( $raw, true );

	if ( ! is_array( $data ) ) {
		wp_send_json_error( 'Malformed status file.' );
	}

	// If status is "running" but PID file is gone, the process ended.
	if ( 'running' === ( $data['status'] ?? '' ) && ! file_exists( BULK_IMPORT_PID_FILE ) ) {
		$data['status'] = 'complete';
	}

	wp_send_json_success( $data );
}
