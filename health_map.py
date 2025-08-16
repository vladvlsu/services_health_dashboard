#!/usr/bin/env python3

import requests
import time
import json
import os
import sys
import logging
import threading
import queue
import signal
import socket
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Optional, Dict
from collections import Counter
from urllib.parse import urlparse

# Rich imports for beautiful terminal output
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.tree import Tree

@dataclass
class Service:
    name: str
    url: str
    timeout: int = 10
    expected_status: int = 200

@dataclass
class NetworkInfo:
    public_ip: str
    location: str
    isp: str
    dns_servers: List[str]
    dns_response_time: Optional[float]

@dataclass
class ServiceStatus:
    name: str
    url: str
    is_healthy: bool
    status_code: Optional[int]
    response_time: Optional[float]
    error: Optional[str]
    last_checked: datetime
    dns_resolution_time: Optional[float] = None
    server_ip: Optional[str] = None

class HealthMap:
    def __init__(self, config_file: str = 'services.json'):
        self.config_file = config_file
        self.services = []
        self.current_statuses: Dict[str, ServiceStatus] = {}
        self.status_queue = queue.Queue()
        self.running = False
        self.check_interval = 10  # Default check interval in seconds
        self.network_info: Optional[NetworkInfo] = None
        self.animation_frame = 0
        self.blink_state = True  # For blinking animation

        # Minimal adaptive intervals
        self.network_refresh_counter = 0
        self.network_refresh_interval = 10   # Network info every 10 cycles
        self.services_refresh_counter = 0
        self.services_refresh_interval = 3   # Services every 3 cycles
        self.system_refresh_counter = 0
        self.system_refresh_interval = 6     # System health every 6 cycles

        # Rich console setup with pale green theme
        self.console = Console()
        self.pale_green = "#afff87"  # Pale green color
        self.pale_green_bg = "#afffaf"  # Pale green background
        self.success_color = "bright_green"
        self.error_color = "bright_red"
        self.warning_color = "bright_yellow"

        # Cached system metrics to avoid expensive polling each frame
        self._sys_metrics = {'cpu': 25.0, 'mem': 45.0, 'disk': 35.0}
        self._sys_metrics_last_ts = 0.0
        self._sys_metrics_ttl = 3.0  # seconds

        # Smooth blink phase for fading indicators
        self.blink_phase = 0
        self.blink_period = 6

        # Subdomain discovery state (background)
        self.subdomains_map: Dict[str, List[str]] = {}
        self.subdomains_ts: Dict[str, float] = {}
        self.subdomains_ttl: float = 24 * 60 * 60  # 24h TTL
        self.subdomains_lock = threading.Lock()
        self.subdomains_inflight = set()

        # Subdomain health probing state (background)
        self.sub_health_map: Dict[str, Dict[str, ServiceStatus]] = {}
        self.sub_health_ts: Dict[str, float] = {}
        self.sub_health_ttl: float = 15 * 60  # 15 minutes
        self.sub_health_lock = threading.Lock()
        self.sub_health_inflight = set()

        # Domain info cache (origin IP, org/ISP, geo)
        self.domain_info_map: Dict[str, dict] = {}
        self.domain_info_ts: Dict[str, float] = {}
        self.domain_info_ttl: float = 24 * 60 * 60  # 24h TTL
        self.domain_info_lock = threading.Lock()
        self.domain_info_inflight = set()

        self._setup_logging()
        self._show_startup_animation()
        self.load_config()
        self._setup_signal_handlers()
        self._gather_network_info()

    def _setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.WARNING,  # Only show warnings and errors
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('health_map.log')  # Only log to file, not console
            ]
        )
        self.logger = logging.getLogger(__name__)

    def _show_startup_animation(self):
        """Show a minimal transient pixel loading animation during startup."""
        try:
            self._startup_anim_stop = threading.Event()

            def _worker():
                frames = ["dim white", "white", "bold white", "white"]
                i = 0
                # transient=True to auto-clear when stopped
                with Live(Align.center(Text("•", style="dim white")), refresh_per_second=8, console=self.console, transient=True) as live:
                    while not self._startup_anim_stop.is_set():
                        style = frames[i % len(frames)]
                        t = Text("HEALTH MAP ", style=f"bold {self.pale_green}")
                        t.append("•", style=style)
                        live.update(Align.center(t))
                        i += 1
                        time.sleep(0.12)

            self._startup_anim_thread = threading.Thread(target=_worker, daemon=True)
            self._startup_anim_thread.start()
        except Exception:
            # Fallback: ensure clean screen even if animation fails
            self.console.clear()

    def _stop_startup_animation(self):
        """Stop startup animation and clear the console before main UI."""
        try:
            ev = getattr(self, "_startup_anim_stop", None)
            th = getattr(self, "_startup_anim_thread", None)
            if ev:
                ev.set()
            if th and th.is_alive():
                th.join(timeout=0.5)
        except Exception:
            pass
        # Clear any residual artifacts
        try:
            self.console.clear()
        except Exception:
            pass

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info("Shutdown signal received. Stopping health monitoring...")
        self.running = False

    def _gather_network_info(self):
        """Gather network information silently"""
        # Get network info without status messages
        ip_info = self._get_public_ip_info()
        dns_servers = self._get_dns_servers()
        dns_response_time = self._test_dns_response_time()

        self.network_info = NetworkInfo(
            public_ip=ip_info['ip'],
            location=ip_info['location'],
            isp=ip_info['isp'],
            dns_servers=dns_servers,
            dns_response_time=dns_response_time
        )

    def _get_public_ip_info(self) -> Dict[str, str]:
        """Get public IP information with multiple fallback services"""
        # List of IP detection services with their parsers
        ip_services = [
            {
                'name': 'ipapi.co',
                'url': 'https://ipapi.co/json/',
                'timeout': 8,
                'parser': self._parse_ipapi_response
            },
            {
                'name': 'api.myip.com',
                'url': 'https://api.myip.com',
                'timeout': 8,
                'parser': self._parse_myip_response
            },
            {
                'name': 'ipify.org',
                'url': 'https://api.ipify.org?format=json',
                'timeout': 5,
                'parser': self._parse_ipify_response
            },
            {
                'name': 'httpbin.org',
                'url': 'https://httpbin.org/ip',
                'timeout': 5,
                'parser': self._parse_httpbin_response
            }
        ]

        # Try each service with retry logic
        for service in ip_services:
            for attempt in range(2):  # 2 attempts per service
                try:
                    # Silent IP detection
                    response = requests.get(
                        service['url'],
                        timeout=service['timeout'],
                        headers={'User-Agent': 'HealthMap/1.0'}
                    )

                    if response.status_code == 200:
                        ip_info = service['parser'](response.json())
                        if ip_info['ip'] != 'Unknown':
                            return ip_info

                except requests.exceptions.Timeout:
                    pass  # Silent failure
                except requests.exceptions.ConnectionError:
                    pass  # Silent failure
                except Exception as e:
                    pass  # Silent failure

                # Small delay between retries
                if attempt == 0:
                    time.sleep(1)

        # All services failed - return fallback silently
        return {
            'ip': 'Unknown',
            'location': 'Unknown',
            'isp': 'Unknown ISP'
        }

    def _parse_ipapi_response(self, data: dict) -> Dict[str, str]:
        """Parse ipapi.co response"""
        return {
            'ip': data.get('ip', 'Unknown'),
            'location': f"{data.get('city', 'Unknown')}, {data.get('region', 'Unknown')}, {data.get('country_name', 'Unknown')}",
            'isp': data.get('org', 'Unknown ISP')
        }

    def _parse_myip_response(self, data: dict) -> Dict[str, str]:
        """Parse api.myip.com response"""
        return {
            'ip': data.get('ip', 'Unknown'),
            'location': f"{data.get('city', 'Unknown')}, {data.get('region', 'Unknown')}, {data.get('country', 'Unknown')}",
            'isp': data.get('isp', 'Unknown ISP')
        }

    def _parse_ipify_response(self, data: dict) -> Dict[str, str]:
        """Parse ipify.org response (IP only)"""
        return {
            'ip': data.get('ip', 'Unknown'),
            'location': 'Unknown',
            'isp': 'Unknown ISP'
        }

    def _parse_httpbin_response(self, data: dict) -> Dict[str, str]:
        """Parse httpbin.org response (IP only)"""
        return {
            'ip': data.get('origin', 'Unknown'),
            'location': 'Unknown',
            'isp': 'Unknown ISP'
        }

    def _get_dns_servers(self) -> List[str]:
        """Get configured DNS servers"""
        try:
            if os.name == 'posix':  # Unix/Linux/macOS
                with open('/etc/resolv.conf', 'r') as f:
                    dns_servers = []
                    for line in f:
                        if line.startswith('nameserver'):
                            dns_servers.append(line.split()[1])
                    return dns_servers[:3]  # Return first 3
            else:
                # Windows fallback
                return ['8.8.8.8', '1.1.1.1']
        except:
            return ['8.8.8.8', '1.1.1.1']

    def _test_dns_response_time(self) -> Optional[float]:
        """Test DNS response time"""
        try:
            start_time = time.time()
            socket.gethostbyname('google.com')
            return (time.time() - start_time) * 1000  # Convert to milliseconds
        except:
            return None

    def _test_ping(self, host: str) -> Optional[float]:
        """Test ping time to a specific host"""
        try:
            if os.name == 'posix':  # Unix/Linux/macOS
                result = subprocess.run(['ping', '-c', '1', '-W', '2000', host],
                                      capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    # Parse ping time from output
                    for line in result.stdout.split('\n'):
                        if 'time=' in line:
                            time_part = line.split('time=')[1].split()[0]
                            ping_time = float(time_part)
                            self._last_ping = ping_time  # Store for network health
                            return ping_time
            return None
        except:
            return None

    def _assess_connection_quality(self, ping_google: Optional[float], ping_cloudflare: Optional[float]) -> str:
        """Assess overall connection quality"""
        pings = [p for p in [ping_google, ping_cloudflare] if p is not None]
        if not pings:
            return "UNKNOWN"

        avg_ping = sum(pings) / len(pings)
        if avg_ping < 30:
            return "EXCELLENT"
        elif avg_ping < 50:
            return "GOOD"
        elif avg_ping < 100:
            return "FAIR"
        else:
            return "POOR"

    def _get_cpu_usage(self) -> float:
        """Get CPU usage percentage"""
        try:
            if os.name == 'posix':
                # Use top command for CPU usage
                result = subprocess.run(['top', '-l', '1', '-n', '0'],
                                      capture_output=True, text=True, timeout=2)
                for line in result.stdout.split('\n'):
                    if 'CPU usage:' in line:
                        # Parse CPU usage from macOS top output
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if 'user' in part and i > 0:
                                return float(parts[i-1].replace('%', ''))
            return 25.0  # Fallback
        except:
            return 25.0

    def _get_memory_usage(self) -> float:
        """Get memory usage percentage"""
        try:
            if os.name == 'posix':
                result = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    # Simple estimation for demo
                    return 45.0  # Fallback estimation
            return 45.0
        except:
            return 45.0

    def _get_disk_usage(self) -> float:
        """Get disk usage percentage"""
        try:
            if os.name == 'posix':
                result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=2)
                for line in result.stdout.split('\n')[1:]:
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 5:
                            usage_str = parts[4].replace('%', '')
                            return float(usage_str)
            return 35.0  # Fallback
        except:
            return 35.0

    def _get_system_metrics(self):
        """Return cached CPU, memory, and disk usage with TTL to reduce polling cost."""
        now = time.time()
        if now - self._sys_metrics_last_ts >= self._sys_metrics_ttl:
            cpu_usage = self._get_cpu_usage()
            memory_usage = self._get_memory_usage()
            disk_usage = self._get_disk_usage()
            self._sys_metrics = {'cpu': cpu_usage, 'mem': memory_usage, 'disk': disk_usage}
            self._sys_metrics_last_ts = now
        return self._sys_metrics['cpu'], self._sys_metrics['mem'], self._sys_metrics['disk']

    def load_config(self):
        """Load services configuration from JSON file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    self.services = [Service(**service) for service in data.get('services', [])]
                # Silent loading - no console output
            except Exception as e:
                self.logger.error(f"Error loading config: {e}")
                self.create_sample_config()
        else:
            self.create_sample_config()

    def create_sample_config(self):
        """Create a sample configuration file"""
        sample_config = {
            "services": [
                {
                    "name": "gemini",
                    "url": "https://gemini.google.com/",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "claudfalre",
                    "url": "https://www.cloudflare.com/",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "gosuslugi",
                    "url": "https://www.gosuslugi.ru/",
                    "timeout": 10,
                    "expected_status": 200
                },                {
                    "name": "whatsapp",
                    "url": "https://www.whatsapp.com/?lang=ru_RU",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "rzd",
                    "url": "https://www.rzd.ru/",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "viber",
                    "url": "https://www.viber.com/ru/",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "truth_social",
                    "url": "https://truthsocial.com/@realDonaldTrump/posts/115012532470689820",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "telegram",
                    "url": "https://web.telegram.org/",
                    "timeout": 10,
                    "expected_status": 200
                },
                {
                    "name": "youtube",
                    "url": "https://www.youtube.com/",
                    "timeout": 10,
                    "expected_status": 200
                },

            ]
        }

        with open(self.config_file, 'w') as f:
            json.dump(sample_config, f, indent=2)

        # Silent config creation
        self.services = [Service(**service) for service in sample_config['services']]

    def check_service_health(self, service: Service) -> ServiceStatus:
        """Check health of a single service"""
        dns_resolution_time = None
        server_ip = None

        try:
            # Extract hostname for DNS resolution
            from urllib.parse import urlparse
            hostname = urlparse(service.url).hostname

            # Test DNS resolution time
            if hostname:
                dns_start = time.time()
                try:
                    server_ip = socket.gethostbyname(hostname)
                    dns_resolution_time = (time.time() - dns_start) * 1000
                except:
                    dns_resolution_time = None

            # HTTP request
            start_time = time.time()
            response = requests.get(
                service.url,
                timeout=service.timeout,
                headers={'User-Agent': 'HealthMap/1.0'}
            )
            response_time = (time.time() - start_time) * 1000  # Convert to milliseconds

            is_healthy = response.status_code == service.expected_status

            return ServiceStatus(
                name=service.name,
                url=service.url,
                is_healthy=is_healthy,
                status_code=response.status_code,
                response_time=response_time,
                error=None,
                last_checked=datetime.now(),
                dns_resolution_time=dns_resolution_time,
                server_ip=server_ip
            )

        except requests.exceptions.Timeout:
            return ServiceStatus(
                name=service.name,
                url=service.url,
                is_healthy=False,
                status_code=None,
                response_time=None,
                error="Timeout",
                last_checked=datetime.now(),
                dns_resolution_time=dns_resolution_time,
                server_ip=server_ip
            )

        except requests.exceptions.ConnectionError:
            return ServiceStatus(
                name=service.name,
                url=service.url,
                is_healthy=False,
                status_code=None,
                response_time=None,
                error="Connection Error",
                last_checked=datetime.now(),
                dns_resolution_time=dns_resolution_time,
                server_ip=server_ip
            )

        except Exception as e:
            return ServiceStatus(
                name=service.name,
                url=service.url,
                is_healthy=False,
                status_code=None,
                response_time=None,
                error=str(e)[:50],
                last_checked=datetime.now(),
                dns_resolution_time=dns_resolution_time,
                server_ip=server_ip
            )

    def check_all_services(self) -> List[ServiceStatus]:
        """Check health of all services concurrently without progress display"""
        statuses = []

        # Silent service checking
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_service = {
                executor.submit(self.check_service_health, service): service
                for service in self.services
            }

            for future in as_completed(future_to_service):
                status = future.result()
                statuses.append(status)

        return sorted(statuses, key=lambda x: x.name)

    def _background_health_checker(self):
        """Background thread that continuously checks service health"""
        # Silent background checker

        while self.running:
            try:
                # Check all services
                statuses = self.check_all_services()

                # Update current statuses and queue for display update
                for status in statuses:
                    self.current_statuses[status.name] = status

                # Signal that new data is available
                try:
                    self.status_queue.put_nowait('update')
                except queue.Full:
                    pass  # Skip if queue is full

                # Wait for next check interval
                for _ in range(self.check_interval * 10):  # Check every 0.1s if we should stop
                    if not self.running:
                        break
                    time.sleep(0.1)

            except Exception as e:
                self.logger.error(f"Error in background health checker: {e}")
                time.sleep(1)

    def get_current_statuses(self) -> List[ServiceStatus]:
        """Get current service statuses"""
        return sorted(list(self.current_statuses.values()), key=lambda x: x.name)

    def format_response_time(self, response_time: Optional[float]) -> str:
        """Format response time for display"""
        if response_time is None:
            return "N/A"
        return f"{response_time:.0f}ms" if response_time < 1000 else f"{response_time/1000:.1f}s"

    def _create_compact_header_panel(self) -> Text:
        """Create ultra-compact header"""
        current_time = datetime.now().strftime('%H:%M:%S')

        header_text = Text("HEALTH MAP", style=f"bold {self.pale_green}")
        header_text.append(f" {current_time}", style="dim white")
        # Loading pixel animation (subtle)
        frames = ["dim white", "white", "bold white", "white"]
        style = frames[self.animation_frame % len(frames)]
        header_text.append("  ")
        header_text.append("•", style=style)
        self.animation_frame = (self.animation_frame + 1) % 1000000

        return Align.center(header_text)

    def _create_compact_summary_panel(self, statuses: List[ServiceStatus]) -> Table:
        """Create compact summary without frame"""
        total_services = len(statuses)
        healthy_services = sum(1 for s in statuses if s.is_healthy)

        # Calculate average response time
        response_times = [s.response_time for s in statuses if s.response_time is not None]
        avg_response = sum(response_times) / len(response_times) if response_times else 0

        summary_table = Table(show_header=False, box=None, padding=(0, 1))
        summary_table.add_column("L", style=f"bold {self.pale_green}", width=3)
        summary_table.add_column("Value", style="bright_white", width=12)

        # Add compact header
        summary_table.add_row("STATS", "", style="dim")

        # Service counts
        summary_table.add_row("TOT", str(total_services))
        summary_table.add_row("UP", Text(str(healthy_services), style=self.success_color))
        summary_table.add_row("DN", Text(str(total_services - healthy_services), style=self.error_color if total_services - healthy_services > 0 else "dim"))

        # Health percentage
        health_pct = (healthy_services / total_services * 100) if total_services > 0 else 0
        pct_color = self.success_color if health_pct >= 90 else self.warning_color if health_pct >= 70 else self.error_color
        summary_table.add_row("PCT", Text(f"{health_pct:.0f}%", style=pct_color))

        # Average response time
        if avg_response > 0:
            avg_color = self.success_color if avg_response < 200 else self.warning_color if avg_response < 1000 else self.error_color
            avg_str = f"{avg_response:.0f}ms" if avg_response < 1000 else f"{avg_response/1000:.1f}s"
            summary_table.add_row("AVG", Text(avg_str, style=avg_color))

        return summary_table

    def _create_insights_panel(self, statuses: List[ServiceStatus]) -> Table:
        """Create compact insights panel with key highlights"""
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("L", style=f"bold {self.pale_green}", width=5)
        table.add_column("Value", style="bright_white", width=20, overflow="ellipsis")

        table.add_row("INS", "INSIGHTS", style="dim")

        # Fastest and slowest services by response time
        times = [(s, s.response_time) for s in statuses if s.response_time is not None]
        if times:
            fastest = min(times, key=lambda t: t[1])[0]
            slowest = max(times, key=lambda t: t[1])[0]
            table.add_row("FAST", f"{fastest.name[:14]} {self.format_response_time(fastest.response_time)}")
            table.add_row("SLOW", f"{slowest.name[:14]} {self.format_response_time(slowest.response_time)}")
        else:
            table.add_row("FAST", "N/A")
            table.add_row("SLOW", "N/A")

        # Failures and top error
        failures = [s for s in statuses if not s.is_healthy]
        if failures:
            errors = [s.error for s in failures if s.error]
            common = Counter(errors).most_common(1)
            top_error = common[0][0] if common else "n/a"
            table.add_row("FAIL", f"{len(failures)} ({top_error[:16]})")
        else:
            table.add_row("FAIL", "0")

        # Average DNS time
        dns_times = [s.dns_resolution_time for s in statuses if s.dns_resolution_time is not None]
        if dns_times:
            avg_dns = sum(dns_times) / len(dns_times)
            dns_color = self.success_color if avg_dns < 50 else self.warning_color if avg_dns < 120 else self.error_color
            table.add_row("DNS", Text(f"{avg_dns:.0f}ms", style=dns_color))
        else:
            table.add_row("DNS", "N/A")

        return table

    def _extract_base_domain(self, hostname: Optional[str]) -> Optional[str]:
        """Extract base domain from hostname using simple heuristics (no heavy deps)."""
        if not hostname:
            return None
        labels = hostname.lower().strip('.').split('.')
        if len(labels) < 2:
            return None
        composite_suffixes = {
            "co.uk", "org.uk", "ac.uk", "gov.uk",
            "com.au", "net.au", "org.au",
            "co.jp", "or.jp", "ne.jp",
            "com.br", "com.tr"
        }
        sld_tld = ".".join(labels[-2:])
        if sld_tld in composite_suffixes and len(labels) >= 3:
            return ".".join(labels[-3:])
        return sld_tld

    def _discover_subdomains_worker(self, base_domain: str):
        """Background worker to fetch subdomains from crt.sh and cache them."""
        try:
            url = f"https://crt.sh/?q=%25.{base_domain}&output=json"
            resp = requests.get(url, timeout=6, headers={'User-Agent': 'HealthMap/1.0'})
            subdomains: List[str] = []
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = []
                seen = set()
                for item in data if isinstance(data, list) else []:
                    name_val = item.get('name_value')
                    if not name_val:
                        continue
                    for name in str(name_val).splitlines():
                        name = name.strip().lower()
                        if name.startswith('*.'):
                            name = name[2:]
                        if name and name.endswith(base_domain) and name != base_domain:
                            if name not in seen:
                                seen.add(name)
                                subdomains.append(name)
            with self.subdomains_lock:
                self.subdomains_map[base_domain] = sorted(subdomains)[:1000]
                self.subdomains_ts[base_domain] = time.time()
        except Exception:
            # On failure, mark timestamp to avoid tight refetch loops
            with self.subdomains_lock:
                self.subdomains_map.setdefault(base_domain, [])
                self.subdomains_ts[base_domain] = time.time()
        finally:
            with self.subdomains_lock:
                self.subdomains_inflight.discard(base_domain)

    def _domain_info_worker(self, base_domain: str):
        """Background worker: resolve base domain IP and fetch geo/hoster info with caching."""
        ip = None
        info: Dict[str, Optional[str]] = {
            'ip': None,
            'org': None,
            'isp': None,
            'city': None,
            'country': None,
            'region': None
        }
        try:
            try:
                ip = socket.gethostbyname(base_domain)
                info['ip'] = ip
            except Exception:
                ip = None

            if ip:
                try:
                    url = f"https://ipwho.is/{ip}?fields=ip,continent,country,region,city,org,isp,success,message"
                    resp = requests.get(url, timeout=6, headers={'User-Agent': 'HealthMap/1.0'})
                    if resp.status_code == 200:
                        data = resp.json()
                        success = data.get('success', True)
                        if success:
                            info['org'] = data.get('org') or None
                            info['isp'] = data.get('isp') or None
                            info['city'] = data.get('city') or None
                            info['country'] = data.get('country') or None
                            info['region'] = data.get('region') or None
                except Exception:
                    pass
        finally:
            with self.domain_info_lock:
                self.domain_info_map[base_domain] = info
                self.domain_info_ts[base_domain] = time.time()
                self.domain_info_inflight.discard(base_domain)

    def _maybe_schedule_domain_info(self, statuses: List[ServiceStatus]):
        """Schedule domain info fetch for base domains when TTL expired."""
        bases = set()
        for s in statuses:
            try:
                hostname = urlparse(s.url).hostname
            except Exception:
                hostname = None
            base = self._extract_base_domain(hostname)
            if base:
                bases.add(base)

        now = time.time()
        for base in bases:
            with self.domain_info_lock:
                last = self.domain_info_ts.get(base, 0)
                inflight = base in self.domain_info_inflight
                need = (now - last) > self.domain_info_ttl or base not in self.domain_info_map
                if need and not inflight:
                    self.domain_info_inflight.add(base)
                    t = threading.Thread(target=self._domain_info_worker, args=(base,), daemon=True)
                    t.start()

    def _get_city_code(self, city: str) -> str:
        """Convert city name to compact city code."""
        city_codes = {
            'london': 'LON', 'new york': 'NYC', 'los angeles': 'LAX', 'chicago': 'CHI',
            'san francisco': 'SFO', 'seattle': 'SEA', 'boston': 'BOS', 'washington': 'WAS',
            'miami': 'MIA', 'atlanta': 'ATL', 'dallas': 'DFW', 'houston': 'HOU',
            'denver': 'DEN', 'phoenix': 'PHX', 'las vegas': 'LAS', 'detroit': 'DTT',
            'philadelphia': 'PHL', 'toronto': 'YYZ', 'vancouver': 'YVR', 'montreal': 'YUL',
            'paris': 'PAR', 'amsterdam': 'AMS', 'frankfurt': 'FRA', 'zurich': 'ZUR',
            'madrid': 'MAD', 'barcelona': 'BCN', 'rome': 'ROM', 'milan': 'MIL',
            'berlin': 'BER', 'munich': 'MUC', 'vienna': 'VIE', 'prague': 'PRG',
            'warsaw': 'WAW', 'stockholm': 'STO', 'copenhagen': 'CPH', 'oslo': 'OSL',
            'helsinki': 'HEL', 'dublin': 'DUB', 'manchester': 'MAN', 'glasgow': 'GLA',
            'edinburgh': 'EDI', 'birmingham': 'BHM', 'liverpool': 'LPL', 'bristol': 'BRS',
            'tokyo': 'NRT', 'osaka': 'KIX', 'seoul': 'ICN', 'beijing': 'PEK',
            'shanghai': 'PVG', 'hong kong': 'HKG', 'singapore': 'SIN', 'bangkok': 'BKK',
            'mumbai': 'BOM', 'delhi': 'DEL', 'bangalore': 'BLR', 'chennai': 'MAA',
            'sydney': 'SYD', 'melbourne': 'MEL', 'brisbane': 'BNE', 'perth': 'PER',
            'auckland': 'AKL', 'wellington': 'WLG', 'johannesburg': 'JNB', 'cape town': 'CPT',
            'cairo': 'CAI', 'dubai': 'DXB', 'doha': 'DOH', 'riyadh': 'RUH',
            'tel aviv': 'TLV', 'istanbul': 'IST', 'moscow': 'SVO', 'st petersburg': 'LED',
            'kiev': 'KBP', 'bucharest': 'OTP', 'sofia': 'SOF', 'athens': 'ATH',
            'lisbon': 'LIS', 'porto': 'OPO', 'brussels': 'BRU', 'luxembourg': 'LUX',
            'geneva': 'GVA', 'basel': 'BSL', 'lyon': 'LYS', 'marseille': 'MRS',
            'nice': 'NCE', 'toulouse': 'TLS', 'strasbourg': 'SXB', 'nantes': 'NTE',
            'bordeaux': 'BOD', 'lille': 'LIL', 'rennes': 'RNS', 'montpellier': 'MPL'
        }
        return city_codes.get(city.lower(), city[:3].upper() if city else "")

    def _create_domain_info_panel(self, statuses: List[ServiceStatus]) -> Table:
        """Create DOMAIN INFO panel summarizing origin IP, ORG/ISP, and geo per base domain."""
        # Collect unique base domains
        bases: List[str] = []
        seen = set()
        for s in statuses:
            hostname = None
            try:
                hostname = urlparse(s.url).hostname
            except Exception:
                pass
            base = self._extract_base_domain(hostname)
            if base and base not in seen:
                seen.add(base)
                bases.append(base)
        bases = sorted(bases)

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("DOM", style=f"bold {self.pale_green}", width=18)
        table.add_column("IP", width=14)
        table.add_column("ORG/ISP", width=16, overflow="ellipsis")
        table.add_column("LOC", width=16, overflow="ellipsis")

        # Header
        table.add_row("DINFO", "", "", "", style="dim")

        with self.domain_info_lock:
            for base in bases:
                inflight = base in self.domain_info_inflight
                info = self.domain_info_map.get(base)
                if info is None and inflight:
                    ip_txt = Text("loading", style="dim")
                    org_txt = Text("", style="dim")
                    loc_txt = Text("", style="dim")
                elif info is None:
                    ip_txt = Text("pending", style="dim")
                    org_txt = Text("", style="dim")
                    loc_txt = Text("", style="dim")
                else:
                    ip_disp = info.get('ip') or "n/a"
                    org = info.get('org') or info.get('isp') or "n/a"
                    city = info.get('city') or ""
                    country = info.get('country') or ""
                    
                    # Use city codes for compact display
                    city_code = self._get_city_code(city) if city else ""
                    country_code = country[:2].upper() if country else ""
                    
                    if city_code and country_code:
                        loc = f"{city_code}, {country_code}"
                    else:
                        loc = city_code or country_code or ""
                    
                    ip_txt = Text(ip_disp, style="bright_white")
                    org_txt = Text(org, style="white")
                    loc_txt = Text(loc, style="white")

                table.add_row(base[:18], ip_txt, org_txt, loc_txt)

        return table

    def _probe_hostname(self, hostname: str, timeout: int = 5) -> ServiceStatus:
        """Probe a hostname via HTTPS → HTTP, HEAD→GET fallback, consider 2xx–3xx healthy."""
        dns_resolution_time = None
        server_ip = None
        last_error = None
        try:
            dns_start = time.time()
            server_ip = socket.gethostbyname(hostname)
            dns_resolution_time = (time.time() - dns_start) * 1000
        except Exception:
            pass

        for scheme in ("https", "http"):
            url = f"{scheme}://{hostname}/"
            # HEAD first
            try:
                start = time.time()
                resp = requests.head(url, timeout=timeout, allow_redirects=True, headers={'User-Agent': 'HealthMap/1.0'})
                rt = (time.time() - start) * 1000
                code = resp.status_code
                if code in (405, 501):
                    raise Exception("HEAD not allowed")
                healthy = 200 <= code < 400
                return ServiceStatus(
                    name=hostname,
                    url=url,
                    is_healthy=healthy,
                    status_code=code,
                    response_time=rt,
                    error=None,
                    last_checked=datetime.now(),
                    dns_resolution_time=dns_resolution_time,
                    server_ip=server_ip
                )
            except Exception as e_head:
                last_error = str(e_head)[:50]
                # GET fallback
                try:
                    start = time.time()
                    resp = requests.get(url, timeout=timeout, headers={'User-Agent': 'HealthMap/1.0'})
                    rt = (time.time() - start) * 1000
                    code = resp.status_code
                    healthy = 200 <= code < 400
                    return ServiceStatus(
                        name=hostname,
                        url=url,
                        is_healthy=healthy,
                        status_code=code,
                        response_time=rt,
                        error=None,
                        last_checked=datetime.now(),
                        dns_resolution_time=dns_resolution_time,
                        server_ip=server_ip
                    )
                except Exception as e_get:
                    last_error = str(e_get)[:50]
                    continue

        return ServiceStatus(
            name=hostname,
            url=f"https://{hostname}/",
            is_healthy=False,
            status_code=None,
            response_time=None,
            error=last_error or "No HTTP",
            last_checked=datetime.now(),
            dns_resolution_time=dns_resolution_time,
            server_ip=server_ip
        )

    def _check_subdomains_for_base(self, base_domain: str, limit: int = 20):
        """Check health of a subset of subdomains for a base domain in background."""
        try:
            with self.subdomains_lock:
                subs = list(self.subdomains_map.get(base_domain, []))
            subs = subs[:limit]
            results: Dict[str, ServiceStatus] = {}
            if subs:
                with ThreadPoolExecutor(max_workers=10) as ex:
                    future_to_sub = {ex.submit(self._probe_hostname, sub): sub for sub in subs}
                    for future in as_completed(future_to_sub):
                        sub = future_to_sub[future]
                        try:
                            results[sub] = future.result()
                        except Exception as e:
                            results[sub] = ServiceStatus(
                                name=sub,
                                url=f"https://{sub}/",
                                is_healthy=False,
                                status_code=None,
                                response_time=None,
                                error=str(e)[:50],
                                last_checked=datetime.now(),
                                dns_resolution_time=None,
                                server_ip=None
                            )
            with self.sub_health_lock:
                self.sub_health_map[base_domain] = results
                self.sub_health_ts[base_domain] = time.time()
        finally:
            with self.sub_health_lock:
                self.sub_health_inflight.discard(base_domain)

    def _maybe_schedule_subdomain_health(self, statuses: List[ServiceStatus]):
        """Schedule subdomain health checks when we have subdomains and TTL expired."""
        bases = set()
        for s in statuses:
            try:
                hostname = urlparse(s.url).hostname
            except Exception:
                hostname = None
            base = self._extract_base_domain(hostname)
            if base:
                bases.add(base)

        now = time.time()
        for base in bases:
            with self.subdomains_lock:
                has_subs = bool(self.subdomains_map.get(base))
            if not has_subs:
                continue
            with self.sub_health_lock:
                last = self.sub_health_ts.get(base, 0)
                inflight = base in self.sub_health_inflight
                need = (now - last) > self.sub_health_ttl or base not in self.sub_health_map
                if need and not inflight:
                    self.sub_health_inflight.add(base)
                    t = threading.Thread(target=self._check_subdomains_for_base, args=(base,), daemon=True)
                    t.start()

    def _maybe_schedule_subdomain_discovery(self, statuses: List[ServiceStatus]):
        """Schedule subdomain discovery for base domains that need refresh."""
        bases = set()
        for s in statuses:
            try:
                hostname = urlparse(s.url).hostname
            except Exception:
                hostname = None
            base = self._extract_base_domain(hostname)
            if base:
                bases.add(base)

        now = time.time()
        for base in bases:
            with self.subdomains_lock:
                last = self.subdomains_ts.get(base, 0)
                inflight = base in self.subdomains_inflight
                need = (now - last) > self.subdomains_ttl or base not in self.subdomains_map
                if need and not inflight:
                    self.subdomains_inflight.add(base)
                    t = threading.Thread(target=self._discover_subdomains_worker, args=(base,), daemon=True)
                    t.start()

    def _create_subdomains_panel(self, statuses: List[ServiceStatus]):
        """Render compact domains table with base domains and top subdomains."""
        # Collect bases from current statuses
        bases = []
        seen_b = set()
        for s in statuses:
            hostname = None
            try:
                hostname = urlparse(s.url).hostname
            except Exception:
                pass
            base = self._extract_base_domain(hostname)
            if base and base not in seen_b:
                seen_b.add(base)
                bases.append(base)

        # Show all base domains
        bases = sorted(bases)

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("DOM", style=f"bold {self.pale_green}", width=20)
        table.add_column("SUBS", width=25, overflow="ellipsis")

        # Header
        table.add_row("DOMAINS", "TOP SUBDOMAINS", style="dim")

        with self.subdomains_lock, self.sub_health_lock:
            for base in bases:
                subs = self.subdomains_map.get(base)
                inflight = base in self.subdomains_inflight
                sub_stats = self.sub_health_map.get(base, {})
                measured = len(sub_stats)
                up = sum(1 for st in sub_stats.values() if st.is_healthy)
                avg_ms = None
                rts = [st.response_time for st in sub_stats.values() if st.response_time]
                if rts:
                    avg_ms = sum(rts) / len(rts)

                # Build compact base domain label
                if subs is None:
                    base_label = Text(f"{base} (pending)", style="dim")
                    subs_text = Text("", style="dim")
                elif inflight:
                    base_label = Text(f"{base} (loading)", style="dim")
                    subs_text = Text("", style="dim")
                else:
                    if measured:
                        ratio = (up / measured) * 100
                        color = self.success_color if ratio >= 80 else self.warning_color if ratio >= 50 else self.error_color
                        if avg_ms is not None:
                            base_text = f"{base} ({len(subs)}) {up}/{measured} {avg_ms:.0f}ms"
                        else:
                            base_text = f"{base} ({len(subs)}) {up}/{measured}"
                        base_label = Text(base_text, style=color)
                    else:
                        base_label = Text(f"{base} ({len(subs) if subs else 0})", style="white")

                    # Build compact subdomains list
                    if subs:
                        # Sort subdomains: healthy first, then by response time
                        subs_with_stats = []
                        subs_without_stats = []
                        
                        for sub in subs[:15]:  # Limit to first 15 for processing
                            st = sub_stats.get(sub)
                            if st:
                                subs_with_stats.append((sub, st))
                            else:
                                subs_without_stats.append(sub)
                        
                        # Sort tested subs: healthy first, then by response time
                        subs_with_stats.sort(key=lambda x: (not x[1].is_healthy, x[1].response_time or 9999))
                        
                        # Build compact subdomain text - show all subdomains
                        sub_parts = []
                        
                        # Show all tested subdomains first
                        for sub, st in subs_with_stats:
                            ind_color = self.success_color if st.is_healthy else self.error_color
                            ind_char = "●" if st.is_healthy else "●"
                            
                            # Very compact subdomain display
                            sub_short = sub.replace(f".{base}", "") if sub.endswith(f".{base}") else sub
                            if len(sub_short) > 12:
                                sub_short = sub_short[:9] + "..."
                            
                            if st.response_time and st.response_time < 1000:
                                sub_parts.append(f"{ind_char}{sub_short}({st.response_time:.0f}ms)")
                            elif st.error:
                                error_short = st.error[:4] if len(st.error) > 4 else st.error
                                sub_parts.append(f"{ind_char}{sub_short}({error_short})")
                            else:
                                sub_parts.append(f"{ind_char}{sub_short}")
                        
                        # Show all untested subdomains
                        for sub in subs_without_stats:
                            sub_short = sub.replace(f".{base}", "") if sub.endswith(f".{base}") else sub
                            if len(sub_short) > 12:
                                sub_short = sub_short[:9] + "..."
                            sub_parts.append(f"○{sub_short}")
                        
                        subs_text = Text(" ".join(sub_parts), style="white")
                    else:
                        subs_text = Text("no data", style="dim")

                table.add_row(base_label, subs_text)

        return table

    def _create_network_panel(self) -> Table:
        """Create compact network information without frame"""
        if not self.network_info:
            return Text("Network unavailable", style="dim")

        # Test additional network metrics
        ping_google = self._test_ping("8.8.8.8")
        ping_cloudflare = self._test_ping("1.1.1.1")
        connection_quality = self._assess_connection_quality(ping_google, ping_cloudflare)

        network_table = Table(show_header=False, box=None, padding=(0, 1))
        network_table.add_column("L", style=f"bold {self.pale_green}", width=3)
        network_table.add_column("Value", style="bright_white", width=18)

        # Add compact header
        network_table.add_row("NET", "INFO", style="dim")

        # Compact network info
        ip_short = self.network_info.public_ip.split('.')
        ip_display = f"{ip_short[0]}.{ip_short[1]}.*.{ip_short[3]}" if len(ip_short) == 4 else self.network_info.public_ip
        network_table.add_row("IP", ip_display)

        # Location (city only)
        location_parts = self.network_info.location.split(',')
        city = location_parts[0] if location_parts else "Unknown"
        network_table.add_row("LOC", city[:15])

        # DNS with response time
        dns_primary = self.network_info.dns_servers[0] if self.network_info.dns_servers else "N/A"
        if self.network_info.dns_response_time:
            dns_time = f"{self.network_info.dns_response_time:.0f}ms"
            dns_color = self.success_color if self.network_info.dns_response_time < 50 else self.warning_color
            network_table.add_row("DNS", Text(f"{dns_primary} ({dns_time})", style=dns_color))
        else:
            network_table.add_row("DNS", dns_primary)

        # Ping times
        if ping_google:
            ping_color = self.success_color if ping_google < 50 else self.warning_color if ping_google < 100 else self.error_color
            network_table.add_row("PNG", Text(f"G:{ping_google:.0f}ms", style=ping_color))

        # Connection quality indicator
        if connection_quality in ("EXCELLENT", "GOOD"):
            quality_color = self.success_color
        elif connection_quality == "FAIR":
            quality_color = self.warning_color
        elif connection_quality == "POOR":
            quality_color = self.error_color
        else:  # UNKNOWN or other
            quality_color = "dim"
        network_table.add_row("QTY", Text(connection_quality, style=quality_color))

        return network_table

    def _create_progress_bar(self, percentage: float, width: int = 4) -> str:
        """Create accurate progress bar based on percentage"""
        filled_chars = int((percentage / 100.0) * width)
        empty_chars = width - filled_chars
        return "█" * filled_chars + "▒" * empty_chars

    def _create_health_blocks_panel(self, statuses: List[ServiceStatus]) -> Table:
        """Create compact health blocks without frame"""
        total_services = len(statuses)
        healthy_services = sum(1 for s in statuses if s.is_healthy)

        # Get system health metrics
        cpu_usage, memory_usage, disk_usage = self._get_system_metrics()

        health_table = Table(show_header=False, box=None, padding=(0, 1))
        health_table.add_column("Block", width=8, style=f"bold {self.pale_green}")
        health_table.add_column("Status", width=12)

        # Add compact header
        health_table.add_row("HEALTH", "STATUS", style="dim")

        # Service health block
        health_pct = (healthy_services / total_services * 100) if total_services > 0 else 0
        service_indicator = self._create_progress_bar(health_pct)
        service_color = self.success_color if health_pct >= 90 else self.warning_color if health_pct >= 70 else self.error_color
        health_table.add_row("SERVICES", Text(f"{service_indicator} {health_pct:.0f}%", style=service_color))

        # CPU health block
        cpu_indicator = self._create_progress_bar(cpu_usage)
        cpu_color = self.success_color if cpu_usage < 50 else self.warning_color if cpu_usage < 85 else self.error_color
        health_table.add_row("CPU", Text(f"{cpu_indicator} {cpu_usage:.0f}%", style=cpu_color))

        # Memory health block
        mem_indicator = self._create_progress_bar(memory_usage)
        mem_color = self.success_color if memory_usage < 60 else self.warning_color if memory_usage < 90 else self.error_color
        health_table.add_row("MEMORY", Text(f"{mem_indicator} {memory_usage:.0f}%", style=mem_color))

        # Disk health block
        disk_indicator = self._create_progress_bar(disk_usage)
        disk_color = self.success_color if disk_usage < 70 else self.warning_color if disk_usage < 95 else self.error_color
        health_table.add_row("DISK", Text(f"{disk_indicator} {disk_usage:.0f}%", style=disk_color))

        # Network health block (based on ping quality)
        net_health = 100 if hasattr(self, '_last_ping') and self._last_ping < 50 else 75 if hasattr(self, '_last_ping') and self._last_ping < 100 else 50
        net_indicator = self._create_progress_bar(net_health)
        net_color = self.success_color if net_health >= 90 else self.warning_color if net_health >= 70 else self.error_color
        health_table.add_row("NETWORK", Text(f"{net_indicator} {net_health}%", style=net_color))

        return health_table

    def _categorize_services(self, statuses: List[ServiceStatus]) -> Dict[str, List[ServiceStatus]]:
        """Categorize services into network and application services"""
        network_services = []
        app_services = []

        # Define network infrastructure services (expanded list)
        network_keywords = ['cloudflare', 'claudfalre', 'dns', 'cdn', 'proxy', 'gateway', 'akamai', 'fastly']

        for status in statuses:
            service_name_lower = status.name.lower()
            if any(keyword in service_name_lower for keyword in network_keywords):
                network_services.append(status)
            else:
                app_services.append(status)

        return {
            'network': sorted(network_services, key=lambda x: x.name),
            'applications': sorted(app_services, key=lambda x: x.name)
        }



    def _create_service_section(self, title: str, services: List[ServiceStatus], show_header: bool = True) -> Table:
        """Create a service section with title and services"""
        table = Table(show_header=show_header, box=None, padding=(0, 1))

        table.add_column("", width=1, justify="center", style=f"bold {self.pale_green}")
        table.add_column("SERVICE", style=f"bold {self.pale_green}", width=12)
        table.add_column("STAT", width=4, style=f"bold {self.pale_green}")
        table.add_column("TIME", width=6, justify="right", style=f"bold {self.pale_green}")
        table.add_column("CODE", width=4, justify="center", style=f"bold {self.pale_green}")
        table.add_column("ERROR", style=f"bold {self.pale_green}", width=18, overflow="ellipsis")

        # Add section title if not showing header
        if not show_header:
            table.add_row("", Text(title, style=f"bold {self.pale_green}"), "", "", "", "", style="dim")

        # Toggle blink state for animation
        self.blink_state = not self.blink_state

        for i, status in enumerate(services):
            # Blinking status indicator
            if status.is_healthy:
                if self.blink_state:
                    indicator = Text("●", style=f"bold {self.success_color}")
                else:
                    indicator = Text("●", style=f"dim {self.success_color}")
                status_text = Text("UP", style=self.success_color)
            else:
                if self.blink_state:
                    indicator = Text("●", style=f"bold {self.error_color}")
                else:
                    indicator = Text("○", style=f"bold {self.error_color}")
                status_text = Text("DN", style=self.error_color)

            # Response time formatting
            if status.response_time:
                if status.response_time < 1000:
                    time_str = f"{status.response_time:.0f}ms"
                else:
                    time_str = f"{status.response_time/1000:.1f}s"

                if status.response_time < 200:
                    time_color = self.success_color
                elif status.response_time < 1000:
                    time_color = self.warning_color
                else:
                    time_color = self.error_color
                response_text = Text(time_str, style=time_color)
            else:
                response_text = Text("N/A", style="dim")

            # Status code formatting
            code = str(status.status_code) if status.status_code else "N/A"
            if status.status_code:
                if 200 <= status.status_code < 300:
                    code_color = self.success_color
                elif 300 <= status.status_code < 400:
                    code_color = self.warning_color
                else:
                    code_color = self.error_color
                code_text = Text(code, style=code_color)
            else:
                code_text = Text("N/A", style="dim")

            # Error message
            error_msg = (status.error[:15] + "..") if status.error and len(status.error) > 17 else (status.error or "")

            table.add_row(
                indicator,
                status.name[:12],
                status_text,
                response_text,
                code_text,
                error_msg
            )

            # Add spacing between entries (but not after last entry in section)
            if i < len(services) - 1:
                table.add_row("", "", "", "", "", "")

        return table

    def _create_minimal_services_table(self, statuses: List[ServiceStatus]) -> Table:
        """Create minimalist services table with adaptive spacing"""
        table = Table(show_header=True, box=None, padding=(0, 2))  # Proper margins

        # Minimal columns with proper spacing
        table.add_column("", width=1, justify="center", style=f"bold {self.pale_green}")
        table.add_column("SERVICE", style=f"bold {self.pale_green}", width=12)
        table.add_column("STAT", width=4, style=f"bold {self.pale_green}")
        table.add_column("TIME", width=6, justify="right", style=f"bold {self.pale_green}")
        table.add_column("CODE", width=4, justify="center", style=f"bold {self.pale_green}")
        table.add_column("ERROR", style=f"bold {self.pale_green}", width=15, overflow="ellipsis")

        # Smooth fading blink phase (0..blink_period-1)
        self.blink_phase = (self.blink_phase + 1) % self.blink_period

        for i, status in enumerate(statuses):
            # Fading status indicator
            phase = self.blink_phase
            if status.is_healthy:
                if phase in (0, 5):
                    style = f"bold {self.success_color}"
                elif phase in (1, 4):
                    style = self.success_color
                else:
                    style = f"dim {self.success_color}"
                indicator = Text("●", style=style)
                status_text = Text("UP", style=self.success_color)
            else:
                if phase in (0, 5):
                    style = f"bold {self.error_color}"
                elif phase in (1, 4):
                    style = self.error_color
                else:
                    style = f"dim {self.error_color}"
                indicator = Text("●", style=style)
                status_text = Text("DN", style=self.error_color)

            # Clean response time
            if status.response_time:
                time_str = f"{status.response_time:.0f}ms" if status.response_time < 1000 else f"{status.response_time/1000:.1f}s"
                time_color = self.success_color if status.response_time < 300 else self.warning_color if status.response_time < 1000 else self.error_color
                response_text = Text(time_str, style=time_color)
            else:
                response_text = Text("N/A", style="dim")

            # Clean status code
            code = str(status.status_code) if status.status_code else "N/A"
            if status.status_code and 200 <= status.status_code < 300:
                code_text = Text(code, style=self.success_color)
            elif status.status_code:
                code_text = Text(code, style=self.error_color)
            else:
                code_text = Text("N/A", style="dim")

            # Clean error message
            error_msg = (status.error[:12] + "..") if status.error and len(status.error) > 14 else (status.error or "")

            table.add_row(
                indicator,
                status.name[:12],
                status_text,
                response_text,
                code_text,
                error_msg
            )

            # Adaptive spacing - only between groups of 3 services
            if i < len(statuses) - 1 and (i + 1) % 3 == 0:
                table.add_row("", "", "", "", "", "")

        return table

    def display_dashboard(self, statuses: List[ServiceStatus]):
        """Display ultra-compact dashboard"""
        self.console.clear()

        # Create minimalist layout with adaptive spacing
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),      # Minimal header
            Layout(name="spacer1", size=1),     # Adaptive spacing
            Layout(name="info", size=6),        # Compact info section
            Layout(name="spacer2", size=1),     # Adaptive spacing
            Layout(name="services")             # Services with remaining space
        )

        # Split info section into ultra-tight columns with minimal spacing
        layout["info"].split_row(
            Layout(name="network", ratio=1),
            Layout(name="health", ratio=1),
            Layout(name="summary", ratio=1),
            Layout(name="insights", ratio=1),
            Layout(name="domains", ratio=2),
            Layout(name="dinfo", ratio=1)
        )

        # Add empty spacers for clean separation
        layout["spacer1"].update(Text(""))
        layout["spacer2"].update(Text(""))

        # Trigger subdomain discovery in background (non-blocking)
        self._maybe_schedule_subdomain_discovery(statuses)
        # Trigger subdomain health checks in background (non-blocking)
        self._maybe_schedule_subdomain_health(statuses)
        # Trigger domain info fetch in background (non-blocking)
        self._maybe_schedule_domain_info(statuses)

        # Populate layout with minimal design
        layout["header"].update(self._create_compact_header_panel())
        layout["network"].update(self._create_network_panel())
        layout["health"].update(self._create_health_blocks_panel(statuses))
        layout["summary"].update(self._create_compact_summary_panel(statuses))
        layout["insights"].update(self._create_insights_panel(statuses))
        layout["domains"].update(self._create_subdomains_panel(statuses))
        layout["dinfo"].update(self._create_domain_info_panel(statuses))

        # Minimalist services table
        services_table = self._create_minimal_services_table(statuses)
        layout["services"].update(services_table)

        # Render once
        self.console.print(layout)

    def run_once(self):
        """Run a single health check and render the dashboard once."""
        # Perform health checks while the startup animation is visible
        try:
            statuses = self.check_all_services()
            for status in statuses:
                self.current_statuses[status.name] = status
        finally:
            # Ensure the animation is stopped before first render
            self._stop_startup_animation()

        # Render once
        self.display_dashboard(statuses)

    def run_continuous(self, interval: int = 10):
        """Run continuous monitoring with a live-updating dashboard."""
        # Sanitize interval
        try:
            self.check_interval = max(1, int(interval))
        except Exception:
            self.check_interval = 10

        # Initial check while startup animation is visible
        try:
            statuses = self.check_all_services()
            for status in statuses:
                self.current_statuses[status.name] = status
        finally:
            # Stop startup animation before entering the UI loop
            self._stop_startup_animation()

        # Start background checker
        self.running = True
        bg_thread = threading.Thread(target=self._background_health_checker, daemon=True)
        bg_thread.start()

        try:
            # Live UI loop
            with Live(Text(""), refresh_per_second=8, console=self.console) as live:
                while self.running:
                    # Create minimalist layout with adaptive spacing
                    layout = Layout()
                    layout.split_column(
                        Layout(name="header", size=1),
                        Layout(name="spacer1", size=1),
                        Layout(name="info", size=6),
                        Layout(name="spacer2", size=1),
                        Layout(name="services")
                    )

                    # Split info section into ultra-tight columns with minimal spacing
                    layout["info"].split_row(
                        Layout(name="network", ratio=1),
                        Layout(name="health", ratio=1),
                        Layout(name="summary", ratio=1),
                        Layout(name="insights", ratio=1),
                        Layout(name="domains", ratio=2),
                        Layout(name="dinfo", ratio=1)
                    )

                    # Add empty spacers for clean separation
                    layout["spacer1"].update(Text(""))
                    layout["spacer2"].update(Text(""))

                    # Header
                    layout["header"].update(self._create_compact_header_panel())

                    # Network info refreshes less frequently
                    self.network_refresh_counter += 1
                    if self.network_refresh_counter >= self.network_refresh_interval:
                        self._gather_network_info()
                        self.network_refresh_counter = 0
                    layout["network"].update(self._create_network_panel())

                    # System health refreshes at medium frequency
                    self.system_refresh_counter += 1
                    if self.system_refresh_counter >= self.system_refresh_interval:
                        self.system_refresh_counter = 0
                    layout["health"].update(self._create_health_blocks_panel(self.get_current_statuses()))

                    # Services refresh more frequently
                    self.services_refresh_counter += 1
                    if self.services_refresh_counter >= self.services_refresh_interval:
                        statuses = self.get_current_statuses()
                        self.services_refresh_counter = 0
                    else:
                        statuses = self.get_current_statuses()

                    # Trigger subdomain discovery and health after statuses refresh
                    self._maybe_schedule_subdomain_discovery(statuses)
                    self._maybe_schedule_subdomain_health(statuses)
                    self._maybe_schedule_domain_info(statuses)

                    layout["summary"].update(self._create_compact_summary_panel(statuses))
                    layout["insights"].update(self._create_insights_panel(statuses))
                    layout["domains"].update(self._create_subdomains_panel(statuses))
                    layout["dinfo"].update(self._create_domain_info_panel(statuses))

                    # Minimalist services table
                    services_table = self._create_minimal_services_table(statuses)
                    layout["services"].update(services_table)

                    # Update live display
                    live.update(layout)

                    # Wait for status updates or user input with timeout
                    try:
                        self.status_queue.get(timeout=0.5)
                    except queue.Empty:
                        pass

                    # Check for user input (non-blocking)
                    if self._check_user_input():
                        continue
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            try:
                if bg_thread.is_alive():
                    bg_thread.join(timeout=1.0)
            except Exception:
                pass
            self.console.print(f"\n[{self.pale_green}][>>] Goodbye! Health monitoring stopped.[/{self.pale_green}]")

    def _check_user_input(self) -> bool:
        """Check for user input without blocking"""
        try:
            import select
            import sys

            # Check if input is available (Unix/Linux/macOS)
            if hasattr(select, 'select'):
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if ready:
                    user_input = sys.stdin.readline().strip().lower()
                    if user_input == 'r':
                        # Force immediate refresh
                        statuses = self.check_all_services()
                        for status in statuses:
                            self.current_statuses[status.name] = status
                        return True
                    elif user_input == 'i':
                        # Change check interval
                        try:
                            new_interval = int(input("Enter new check interval (seconds): "))
                            if new_interval > 0:
                                self.check_interval = new_interval
                                print(f"Check interval updated to {new_interval}s")
                            else:
                                print("Invalid interval. Must be positive.")
                        except ValueError:
                            print("Invalid input. Please enter a number.")
                        return True
        except ImportError:
            # Fallback for systems without select (like Windows)
            pass

        return False

def main():
    """Main entry point for the Health Map CLI"""
    import argparse

    # Create a temporary console for startup
    console = Console()

    parser = argparse.ArgumentParser(description="Health Map CLI Dashboard")
    parser.add_argument("--config", "-c", default="services.json", help="Path to services config file")
    parser.add_argument("--once", "-o", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", "-i", type=int, default=10, help="Health check interval in seconds (default: 10)")

    args = parser.parse_args()

    try:
        # Skip welcome message for clean startup

        health_map = HealthMap(args.config)

        if not health_map.services:
            # Ensure startup animation is stopped on early exit
            try:
                health_map._stop_startup_animation()
            except Exception:
                pass
            console.print("[red][X] No services configured. Please edit the config file and try again.[/red]")
            return

        if args.once:
            health_map.run_once()
        else:
            health_map.run_continuous(args.interval)

    except KeyboardInterrupt:
        console.print(f"\n[bright_green][>>] Goodbye![/bright_green]")
    except Exception as e:
        console.print(f"[red][X] Application error: {e}[/red]")
        logging.error(f"Application error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
