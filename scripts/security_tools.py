#!/usr/bin/env python3
"""
Security Tools and Automation Scripts
Run security checks, generate reports, and perform maintenance tasks.
"""
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from phonos_app.helpers.ip_security import IPSecurity
from phonos_app.helpers.audit_log import (
    get_suspicious_activity,
    get_security_events,
    get_audit_logs
)
from phonos_app.helpers.file_scanner import FileQuarantine


def cleanup_expired_blocks():
    """Remove expired IP blocks."""
    print("Cleaning up expired IP blocks...")
    IPSecurity.cleanup_expired_blocks()
    print("✓ Expired blocks removed")


def cleanup_old_threat_scores(days=30):
    """Remove old threat score records."""
    print(f"Cleaning up threat scores older than {days} days...")
    IPSecurity.cleanup_old_threat_scores(days=days)
    print("✓ Old threat scores removed")


def generate_security_report(days=7, output_file=None):
    """Generate comprehensive security report."""
    print(f"Generating security report for last {days} days...")

    # Get data
    blocked_ips = IPSecurity.get_blocked_ips()
    suspicious = get_suspicious_activity(days=days)
    security_events = get_security_events(days=days)
    quarantined = FileQuarantine.list_quarantined_files()

    # Generate report
    report = []
    report.append("=" * 80)
    report.append(f"SECURITY REPORT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Period: Last {days} days")
    report.append("=" * 80)
    report.append("")

    # Summary
    report.append("SUMMARY")
    report.append("-" * 80)
    report.append(f"Blocked IPs: {len(blocked_ips)}")
    report.append(f"Suspicious Activities: {len(suspicious)}")
    report.append(f"Security Events: {len(security_events)}")
    report.append(f"Quarantined Files: {len(quarantined)}")
    report.append("")

    # Blocked IPs
    report.append("BLOCKED IP ADDRESSES")
    report.append("-" * 80)
    if blocked_ips:
        for ip in blocked_ips[:20]:  # Top 20
            report.append(f"  {ip['ip_address']:<20} - {ip['reason']}")
            report.append(f"    Blocked: {ip['blocked_at']} | Until: {ip.get('blocked_until', 'Permanent')}")
            report.append(f"    Threat Score: {ip['threat_score']}")
    else:
        report.append("  No IPs currently blocked")
    report.append("")

    # Suspicious Activity
    report.append("SUSPICIOUS ACTIVITY")
    report.append("-" * 80)
    if suspicious:
        for activity in suspicious[:20]:
            report.append(f"  Type: {activity['type']}")
            report.append(f"    Severity: {activity['severity']}")
            if 'count' in activity:
                report.append(f"    Count: {activity['count']}")
            report.append("")
    else:
        report.append("  No suspicious activity detected")
    report.append("")

    # Security Events
    report.append("RECENT SECURITY EVENTS")
    report.append("-" * 80)
    if security_events:
        event_counts = {}
        for event in security_events:
            event_type = event['event_type']
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

        for event_type, count in sorted(event_counts.items(), key=lambda x: x[1], reverse=True):
            report.append(f"  {event_type:<40} {count:>6} events")
    else:
        report.append("  No security events in this period")
    report.append("")

    # Quarantined Files
    report.append("QUARANTINED FILES")
    report.append("-" * 80)
    if quarantined:
        for file in quarantined:
            report.append(f"  ID: {file['quarantine_id']}")
            report.append(f"    Size: {file['size']} bytes")
            report.append(f"    Quarantined: {datetime.fromtimestamp(file['quarantined_at'])}")
            report.append("")
    else:
        report.append("  No files in quarantine")
    report.append("")

    report.append("=" * 80)
    report.append("END OF REPORT")
    report.append("=" * 80)

    report_text = "\n".join(report)

    # Output
    if output_file:
        with open(output_file, 'w') as f:
            f.write(report_text)
        print(f"✓ Report saved to {output_file}")
    else:
        print(report_text)


def scan_recent_logins(minutes=60):
    """Scan recent logins for suspicious patterns."""
    print(f"Scanning logins from last {minutes} minutes...")

    start_time = datetime.utcnow() - timedelta(minutes=minutes)

    logs = get_audit_logs(
        event_type='auth.login.failure',
        start_date=start_time,
        limit=1000
    )

    # Group by IP
    ip_failures = {}
    for log in logs:
        ip = log.get('ip_address')
        if ip:
            ip_failures[ip] = ip_failures.get(ip, 0) + 1

    # Report high-failure IPs
    suspicious_ips = {ip: count for ip, count in ip_failures.items() if count >= 5}

    if suspicious_ips:
        print(f"\n⚠ Found {len(suspicious_ips)} suspicious IPs:")
        for ip, count in sorted(suspicious_ips.items(), key=lambda x: x[1], reverse=True):
            print(f"  {ip:<20} {count:>4} failed logins")

            # Check if already blocked
            is_blocked, _ = IPSecurity.is_ip_blocked(ip)
            if not is_blocked:
                print(f"    → Consider blocking this IP")
    else:
        print("✓ No suspicious login patterns detected")


def auto_block_attackers(threshold=10, duration_hours=24):
    """Automatically block IPs with excessive failed logins."""
    print(f"Auto-blocking IPs with {threshold}+ failed logins in last hour...")

    start_time = datetime.utcnow() - timedelta(hours=1)

    logs = get_audit_logs(
        event_type='auth.login.failure',
        start_date=start_time,
        limit=10000
    )

    # Count failures by IP
    ip_failures = {}
    for log in logs:
        ip = log.get('ip_address')
        if ip:
            ip_failures[ip] = ip_failures.get(ip, 0) + 1

    # Block IPs over threshold
    blocked_count = 0
    for ip, count in ip_failures.items():
        if count >= threshold:
            # Check if already blocked or whitelisted
            is_blocked, _ = IPSecurity.is_ip_blocked(ip)
            if not is_blocked:
                success = IPSecurity.block_ip(
                    ip,
                    reason=f"Auto-block: {count} failed logins in 1 hour",
                    duration_hours=duration_hours,
                    threat_score=count * 10
                )
                if success:
                    print(f"✓ Blocked {ip} ({count} failed logins)")
                    blocked_count += 1

    if blocked_count == 0:
        print("✓ No IPs needed blocking")
    else:
        print(f"✓ Blocked {blocked_count} IPs")


def check_security_health():
    """Run security health check."""
    print("Running security health check...\n")

    issues = []
    warnings = []

    # Check 1: Blocked IPs
    blocked_ips = IPSecurity.get_blocked_ips()
    if len(blocked_ips) > 100:
        warnings.append(f"Large number of blocked IPs: {len(blocked_ips)}")

    # Check 2: Recent security events
    recent_events = get_security_events(days=1)
    if len(recent_events) > 50:
        warnings.append(f"High number of security events today: {len(recent_events)}")

    # Check 3: Quarantined files
    quarantined = FileQuarantine.list_quarantined_files()
    if len(quarantined) > 10:
        warnings.append(f"Many files in quarantine: {len(quarantined)}")

    # Check 4: Suspicious activity
    suspicious = get_suspicious_activity(days=1)
    if len(suspicious) > 0:
        issues.append(f"Suspicious activity detected: {len(suspicious)} incidents")

    # Report
    if not issues and not warnings:
        print("✓ Security health check PASSED")
        print("  No issues or warnings detected")
        return True
    else:
        print("⚠ Security health check completed with findings:\n")

        if issues:
            print("ISSUES:")
            for issue in issues:
                print(f"  ✗ {issue}")
            print()

        if warnings:
            print("WARNINGS:")
            for warning in warnings:
                print(f"  ⚠ {warning}")
            print()

        return False


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Phonos Security Tools')
    parser.add_argument('command', choices=[
        'cleanup',
        'report',
        'scan-logins',
        'auto-block',
        'health-check',
        'all'
    ], help='Command to run')
    parser.add_argument('--days', type=int, default=7, help='Number of days for reports')
    parser.add_argument('--output', help='Output file for reports')
    parser.add_argument('--threshold', type=int, default=10, help='Threshold for auto-blocking')

    args = parser.parse_args()

    print(f"Phonos Security Tools")
    print(f"Running: {args.command}\n")

    if args.command == 'cleanup' or args.command == 'all':
        cleanup_expired_blocks()
        cleanup_old_threat_scores()
        print()

    if args.command == 'report' or args.command == 'all':
        generate_security_report(days=args.days, output_file=args.output)
        print()

    if args.command == 'scan-logins' or args.command == 'all':
        scan_recent_logins()
        print()

    if args.command == 'auto-block' or args.command == 'all':
        auto_block_attackers(threshold=args.threshold)
        print()

    if args.command == 'health-check' or args.command == 'all':
        check_security_health()
        print()

    print("Done!")


if __name__ == '__main__':
    main()
