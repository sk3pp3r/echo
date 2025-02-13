from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import subprocess
import re
import os
import io
from datetime import datetime
import ipaddress
import json
import csv

app = Flask(__name__, static_url_path='/static')
# Use environment variable for secret key
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev_key')

def validate_target(target):
    """Validate target IP address, hostname, or IP range"""
    # Split multiple targets
    targets = target.split(',')
    
    for t in targets:
        t = t.strip()
        # Check if it's an IP range (e.g., 192.168.1.1-254)
        if '-' in t:
            try:
                # Handle different range formats
                if t.count('.') == 3:  # Format: 192.168.1.1-254
                    start, end = t.rsplit('-', 1)
                    if '.' not in end:  # Convert 192.168.1.1-254 to proper range
                        base = start.rsplit('.', 1)[0]
                        end = f"{base}.{end}"
                else:  # Format: 192.168.1-192.168.2
                    start, end = t.split('-')
                
                # Validate both IPs
                ipaddress.ip_address(start)
                ipaddress.ip_address(end)
            except ValueError:
                return False
        else:
            try:
                # Try as IP address
                ipaddress.ip_address(t)
            except ValueError:
                # Try as hostname
                if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\.-]*[a-zA-Z0-9]$', t):
                    return False
    return True

def validate_ports(ports):
    """Validate port numbers and ranges"""
    # Allow single ports, ranges (1-1000), and comma-separated values
    pattern = r'^[\d,-]+$'
    if not re.match(pattern, ports):
        return False
    
    try:
        for part in ports.split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                if not (0 <= start <= 65535 and 0 <= end <= 65535 and start <= end):
                    return False
            else:
                port = int(part)
                if not (0 <= port <= 65535):
                    return False
        return True
    except ValueError:
        return False

def parse_nmap_output(output):
    """Parse nmap output into structured data"""
    lines = output.split('\n')
    result = {
        'scan_info': {},
        'hosts': []
    }
    
    current_host = None
    
    for line in lines:
        if line.startswith('Nmap scan report for'):
            if current_host:
                result['hosts'].append(current_host)
            current_host = {
                'host': line.split('for ')[-1],
                'ports': []
            }
        elif line.startswith('PORT'):
            continue
        elif '/tcp' in line or '/udp' in line:
            parts = line.split()
            if len(parts) >= 3:
                port_info = {
                    'port': parts[0],
                    'state': parts[1],
                    'service': ' '.join(parts[2:])
                }
                if current_host:
                    current_host['ports'].append(port_info)
    
    if current_host:
        result['hosts'].append(current_host)
    
    return result

def create_csv_content(scan_data):
    """Convert scan data to CSV format"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(['Host', 'Port', 'State', 'Service'])
    
    # Write scan data
    for host in scan_data['hosts']:
        host_address = host['host']
        for port in host['ports']:
            writer.writerow([
                host_address,
                port['port'],
                port['state'],
                port['service']
            ])
    
    return output.getvalue()

# Add this function to parse scan statistics
def parse_scan_stats(output):
    """Parse nmap output for scan statistics"""
    stats = {
        'open_ports': 0,
        'closed_ports': 0,
        'filtered_ports': 0,
        'scan_time': '0',
        'total_ports': 0
    }
    
    lines = output.split('\n')
    for line in lines:
        # Check for individual port states
        if '/tcp' in line or '/udp' in line:
            parts = line.split()
            if len(parts) >= 2:
                state = parts[1]  # 'open', 'closed', or 'filtered'
                if state in ['open', 'closed', 'filtered']:
                    stats[f'{state}_ports'] += 1
                    stats['total_ports'] += 1
        
        # Check for summary lines
        elif 'open port' in line or 'closed port' in line or 'filtered port' in line:
            parts = line.split()
            if len(parts) >= 4:
                count = int(parts[0])
                state = parts[2]  # 'open', 'closed', or 'filtered'
                # Only use summary if we haven't counted individual ports
                if stats['total_ports'] == 0:
                    stats[f'{state}_ports'] = count
                    stats['total_ports'] += count
        
        # Get scan time
        elif 'Nmap done:' in line and 'scanned in' in line:
            time_part = line.split('scanned in')[-1].strip()
            stats['scan_time'] = time_part.strip()
    
    return stats

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scan', methods=['POST'])
def scan():
    target = request.form.get('target', '').strip()
    ports = request.form.get('ports', '').strip()
    
    if not target or not ports:
        flash("Both target and ports are required.")
        return redirect(url_for('index'))
    
    if not validate_target(target):
        flash("Invalid target specified. Please check the format.")
        return redirect(url_for('index'))
    
    if not validate_ports(ports):
        flash("Invalid port specification.")
        return redirect(url_for('index'))
    
    # Build the nmap command with validated input
    cmd = ["nmap", "-p", ports]
    
    # Add target(s)
    if ',' in target:
        # Multiple targets
        cmd.extend(target.split(','))
    elif '-' in target:
        # IP range
        cmd.append(target)
    else:
        # Single target
        cmd.append(target)
    
    try:
        result = subprocess.check_output(
            cmd, 
            stderr=subprocess.STDOUT,
            timeout=300,
            encoding='utf-8'
        )
        result_decoded = result
        scan_stats = parse_scan_stats(result)
    except subprocess.CalledProcessError as e:
        result_decoded = f"Error occurred:\n{e.output}"
        scan_stats = {}
    except subprocess.TimeoutExpired:
        result_decoded = "Scan timed out after 5 minutes"
        scan_stats = {}
    
    return render_template('result.html', 
                         target=target, 
                         ports=ports, 
                         result=result_decoded,
                         scan_stats=scan_stats)

@app.route('/download/<format>/<target>/<ports>')
def download_result(format, target, ports):
    if format not in ['log', 'json', 'csv']:
        format = 'log'  # Default to log format
        
    if not validate_target(target) or not validate_ports(ports):
        flash("Invalid parameters for download")
        return redirect(url_for('index'))
        
    cmd = ["nmap", "-p", ports]
    
    # Add target(s)
    if ',' in target:
        cmd.extend(target.split(','))
    elif '-' in target:
        cmd.append(target)
    else:
        cmd.append(target)
    
    try:
        result = subprocess.check_output(
            cmd, 
            stderr=subprocess.STDOUT,
            timeout=300,
            encoding='utf-8'
        )
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        
        if format == 'json':
            scan_data = parse_nmap_output(result)
            scan_data['metadata'] = {
                'timestamp': timestamp,
                'target': target,
                'ports': ports
            }
            
            buffer = io.BytesIO()
            buffer.write(json.dumps(scan_data, indent=2).encode('utf-8'))
            buffer.seek(0)
            
            return send_file(
                buffer,
                mimetype='application/json',
                as_attachment=True,
                download_name=f'nmap_scan_{target}_{timestamp}.json'
            )
        elif format == 'csv':
            scan_data = parse_nmap_output(result)
            csv_content = create_csv_content(scan_data)
            
            buffer = io.BytesIO()
            buffer.write(csv_content.encode('utf-8'))
            buffer.seek(0)
            
            return send_file(
                buffer,
                mimetype='text/csv',
                as_attachment=True,
                download_name=f'nmap_scan_{target}_{timestamp}.csv'
            )
        else:  # log format (default)
            log_content = f"""Nmap Scan Results
Timestamp: {timestamp}
Target: {target}
Ports: {ports}
{'='*50}

{result}
"""
            buffer = io.BytesIO()
            buffer.write(log_content.encode('utf-8'))
            buffer.seek(0)
            
            return send_file(
                buffer,
                mimetype='text/plain',
                as_attachment=True,
                download_name=f'nmap_scan_{target}_{timestamp}.log'
            )
            
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        flash("Error generating download file")
        return redirect(url_for('index'))

if __name__ == '__main__':
    # Production configuration
    app.run(
        host='0.0.0.0',
        port=8085,
        debug=False
    )
