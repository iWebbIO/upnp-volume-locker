#!/usr/bin/env python3
"""
Volume Locker Pro: A UPnP/DLNA Device Control Tool.

This script discovers UPnP/DLNA media renderer devices on the local network.
It allows users to select a device and perform actions, such as "locking"
the volume to a specific level by repeatedly sending control commands.

It uses the Rich library for a modern, clean command-line interface.
"""

import socket
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

import netifaces
import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# --- Global Rich Console ---
console = Console()

# --- Constants ---
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TIMEOUT = 5  # Seconds to listen for responses
UPNP_NS = {'root': 'urn:schemas-upnp-org:device-1-0'}
DeviceInfo = Dict[str, Any]


# --- Helper Function for SOAP Requests ---
def send_soap_request(
    control_url: str,
    service_type: str,
    action: str,
    arguments: str,
    headers: Optional[Dict[str, str]] = None
) -> Optional[requests.Response]:
    """
    Sends a generic SOAP request to a device's control URL.

    Args:
        control_url: The URL for the service's control endpoint.
        service_type: The URN for the service type.
        action: The action to perform (e.g., 'SetVolume').
        arguments: The XML arguments for the action.
        headers: Optional additional headers.

    Returns:
        The requests.Response object on success, or None on failure.
    """
    if headers is None:
        headers = {}

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <s:Body>
        <u:{action} xmlns:u="{service_type}">
            {arguments}
        </u:{action}>
    </s:Body>
</s:Envelope>"""

    final_headers = {
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPACTION': f'"{service_type}#{action}"',
        **headers
    }

    try:
        response = requests.post(
            control_url,
            data=soap_body.encode('utf-8'),
            headers=final_headers,
            timeout=5
        )
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException:
        # Silently fail for frequent actions to avoid spamming the console
        if action not in ['SetVolume', 'GetVolume']:
            console.log(f"[bold red]Error sending SOAP request for action '{action}'[/]")
        return None


# --- Discovery Functions ---
def discover_devices() -> Dict[str, DeviceInfo]:
    """Discovers UPnP devices on the network with a rich live display."""
    msearch_payload = (
        f'M-SEARCH * HTTP/1.1\r\n'
        f'HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n'
        f'MAN: "ssdp:discover"\r\n'
        f'MX: {SSDP_TIMEOUT}\r\n'
        f'ST: ssdp:all\r\n\r\n'
    ).encode('utf-8')

    locations: Set[str] = set()
    sockets: List[socket.socket] = []

    spinner = Spinner("dots", text=Text("Initializing...", style="cyan"))
    with Live(spinner, refresh_per_second=10, console=console, transient=True) as live:
        try:
            interfaces = netifaces.interfaces()
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] Could not get network interfaces. Is 'netifaces' installed? (`pip install netifaces`). Error: {e}")
            return {}

        live.update(Spinner("dots", text=Text("Binding to network interfaces...", style="cyan")))
        for iface in interfaces:
            try:
                ip_info = netifaces.ifaddresses(iface).get(netifaces.AF_INET)
                if not ip_info:
                    continue

                ip_addr = ip_info[0]['addr']
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.bind((ip_addr, 0))
                sockets.append(sock)
            except (OSError, PermissionError):
                pass  # Ignore interfaces we can't bind to

        if not sockets:
            console.print("[bold red]Error:[/bold red] No available network interfaces for discovery.")
            return {}

        live.update(Spinner("dots", text=Text(f"Sending M-SEARCH discovery packets (waiting {SSDP_TIMEOUT}s)...", style="cyan")))
        for sock in sockets:
            try:
                sock.sendto(msearch_payload, (SSDP_ADDR, SSDP_PORT))
            except Exception as e:
                console.log(f"[yellow]Warning:[/yellow] Could not send M-SEARCH on {sock.getsockname()}: {e}", style="dim")

        start_time = time.time()
        while time.time() - start_time < SSDP_TIMEOUT:
            for sock in sockets:
                sock.settimeout(0.1)
                try:
                    data, _ = sock.recvfrom(2048)
                    response = data.decode('utf-8', errors='ignore')
                    for line in response.splitlines():
                        if line.lower().startswith('location:'):
                            locations.add(line.split(':', 1)[1].strip())
                            live.update(Spinner("dots", text=Text(f"Listening... Found {len(locations)} potential devices.", style="cyan")))
                except socket.timeout:
                    continue
                except Exception as e:
                    console.log(f"[red]Error receiving data:[/red] {e}", style="dim")

        for sock in sockets:
            sock.close()

    if locations:
        console.print(f"\n[green]✔[/green] Found {len(locations)} unique device locations. Fetching details...")
    return parse_device_descriptions(locations)


def parse_device_descriptions(locations: Set[str]) -> Dict[str, DeviceInfo]:
    """Fetches and parses XML from device location URLs to find services."""
    found_devices: Dict[str, DeviceInfo] = {}
    for loc in locations:
        try:
            response = requests.get(loc, timeout=3)
            response.raise_for_status()

            xml_root = ET.fromstring(response.content)
            device_node = xml_root.find('.//root:device', UPNP_NS)
            if device_node is None:
                continue

            friendly_name_node = device_node.find('root:friendlyName', UPNP_NS)
            if friendly_name_node is None or friendly_name_node.text is None:
                continue

            friendly_name = friendly_name_node.text.strip()
            device_info: DeviceInfo = {'location': loc, 'friendly_name': friendly_name, 'services': {}}

            service_list = device_node.find('.//root:serviceList', UPNP_NS)
            if service_list is not None:
                for service in service_list.findall('root:service', UPNP_NS):
                    service_type_node = service.find('root:serviceType', UPNP_NS)
                    control_url_node = service.find('root:controlURL', UPNP_NS)

                    if service_type_node is not None and service_type_node.text is not None and \
                       control_url_node is not None and control_url_node.text is not None:
                        service_type = service_type_node.text
                        base_url = response.url  # Use the final URL after any redirects
                        control_url = urljoin(base_url, control_url_node.text)

                        if 'AVTransport' in service_type:
                            device_info['services']['AVTransport'] = {'type': service_type, 'url': control_url}
                        elif 'RenderingControl' in service_type:
                            device_info['services']['RenderingControl'] = {'type': service_type, 'url': control_url}

            if 'AVTransport' in device_info['services'] or 'RenderingControl' in device_info['services']:
                found_devices[friendly_name] = device_info

        except requests.exceptions.RequestException:
            pass  # Silently ignore connection errors to unresponsive devices
        except Exception as e:
            console.log(f"[yellow]Could not process location {loc}:[/yellow] {e}", style="dim")

    return found_devices


def print_discovered_devices(devices: Dict[str, DeviceInfo]) -> None:
    """Prints the discovered devices in a rich table."""
    if not devices:
        console.print(Panel("[bold yellow]No controllable UPnP/DLNA devices found on the network.[/bold yellow]", border_style="yellow", expand=False))
        return

    table = Table(title="Discovered Controllable Devices", border_style="blue", title_style="bold magenta")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Device Name", style="bold green")
    table.add_column("Capabilities", style="yellow")
    table.add_column("Location", style="dim")

    for idx, (name, info) in enumerate(devices.items()):
        capabilities = []
        if 'AVTransport' in info['services']:
            capabilities.append("▶️ Play Media")
        if 'RenderingControl' in info['services']:
            capabilities.append("🔊 Volume Control")

        table.add_row(str(idx + 1), name, ', '.join(capabilities), info['location'])

    console.print(table)


# --- Control Functions ---
def set_media_and_play(device_info: DeviceInfo, media_url: str) -> bool:
    """Sets the media URI and then sends the play command."""
    av_transport = device_info.get('services', {}).get('AVTransport')
    if not av_transport:
        console.print(f"[bold red]Error:[/bold red] Device '{device_info['friendly_name']}' does not support AVTransport.")
        return False

    console.print(f"Setting media URL on [cyan]{device_info['friendly_name']}[/]...")
    set_uri_args = f"<InstanceID>0</InstanceID><CurrentURI>{media_url}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>"
    if send_soap_request(av_transport['url'], av_transport['type'], 'SetAVTransportURI', set_uri_args) is None:
        console.print("[bold red]❌ Failed to set media URL.[/bold red]")
        return False

    console.print("▶️ Sending PLAY command...")
    play_args = "<InstanceID>0</InstanceID><Speed>1</Speed>"
    if send_soap_request(av_transport['url'], av_transport['type'], 'Play', play_args) is not None:
        console.print(f"[bold green]✔ Play command sent successfully to [cyan]{device_info['friendly_name']}[/].[/bold green]")
        return True
    else:
        console.print("[bold red]❌ Failed to send PLAY command.[/bold red]")
        return False


def set_volume(device_info: DeviceInfo, volume: int) -> bool:
    """Sets the volume of the selected device. Returns True on success."""
    rendering_control = device_info.get('services', {}).get('RenderingControl')
    if not rendering_control:
        return False

    set_vol_args = f"<InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>"
    return send_soap_request(rendering_control['url'], rendering_control['type'], 'SetVolume', set_vol_args) is not None


# --- Volume Lock Threading Logic ---
def volume_setter_worker(
    device_info: DeviceInfo,
    volume: int,
    stop_event: threading.Event,
    status_lock: threading.Lock,
    shared_status: Dict[str, bool],
    interval: float
) -> None:
    """
    WORKER THREAD TARGET: Continuously sets the volume.
    This function should not interact with the console directly. It updates a
    shared status dictionary to communicate its state to the main thread.
    """
    while not stop_event.is_set():
        success = set_volume(device_info, volume)
        with status_lock:
            shared_status['success'] = success

        # Wait for the interval, but be responsive to the stop event.
        stop_event.wait(timeout=interval)


def generate_volume_panel(device_name: str, volume: int, interval: float, success_status: bool) -> Panel:
    """Generates the rich Panel for the live volume display."""
    if success_status:
        status_text = Text("✅ Locked", style="green")
        border_style = "green"
    else:
        status_text = Text("❌ Failed (Device may be off?)", style="red")
        border_style = "red"

    return Panel(
        Text.from_markup(f"""[bold]Device:[/] [cyan]{device_name}[/]
[bold]Volume Lock:[/] [bold magenta]{volume}%[/]
[bold]Status:[/] {status_text}

[dim]The volume is being reset every {interval:.1f}s.
Press [bold]Ctrl+C[/bold] to stop.[/dim]"""),
        title="🔊 Volume Lock Active",
        border_style=border_style,
        width=60
    )


# --- Main Application Logic ---
def main() -> None:
    """Main function to run the device discovery and control interface."""
    console.print(Panel("[bold cyan]Volume Locker Pro[/bold cyan]", subtitle="by iWebbIO", expand=False))

    # Initialize thread-related variables outside the try block for the finally clause
    volume_thread: Optional[threading.Thread] = None
    stop_event = threading.Event()

    try:
        devices = discover_devices()
        devices_list = list(devices.values())

        if not devices:
            print_discovered_devices(devices)
            return

        print_discovered_devices(devices)

        # --- Device Selection ---
        choice = console.input("[bold]Select a device by number to control (or press Enter to exit): [/]")
        if not choice.strip():
            return

        selection = int(choice) - 1
        if not (0 <= selection < len(devices_list)):
            console.print("[bold red]Invalid selection.[/bold red]")
            return

        selected_device = devices_list[selection]
        console.print(f"\n> You selected: [bold cyan]{selected_device['friendly_name']}[/]")

        # --- Action 1: Play Media (optional) ---
        if 'AVTransport' in selected_device['services']:
            play_q = console.input("Do you want to play a sample video? ([bold green]y[/]/[bold red]n[/]): ").lower()
            if play_q == 'y':
                set_media_and_play(selected_device, "http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4")
                time.sleep(2)

        # --- Action 2: Lock Volume ---
        if 'RenderingControl' in selected_device['services']:
            volume_str = console.input("Enter volume level to lock ([bold]0-100[/]), e.g., 0 to mute: ")
            volume = int(volume_str)
            if not (0 <= volume <= 100):
                console.print("[bold red]Volume must be between 0 and 100.[/bold red]")
                return

            # --- Start Volume Lock Threading ---
            status_lock = threading.Lock()
            shared_status = {'success': True}  # Start with a positive assumption
            lock_interval = 1.5

            volume_thread = threading.Thread(
                target=volume_setter_worker,
                args=(selected_device, volume, stop_event, status_lock, shared_status, lock_interval),
                daemon=True
            )
            volume_thread.start()

            # The MAIN THREAD runs the Live display, controlled by the worker's status
            with Live(console=console, screen=True, auto_refresh=False) as live:
                while not stop_event.is_set():
                    with status_lock:
                        current_success_status = shared_status['success']

                    panel = generate_volume_panel(selected_device['friendly_name'], volume, lock_interval, current_success_status)
                    live.update(panel, refresh=True)
                    time.sleep(0.1)  # UI refresh rate

    except (ValueError, IndexError):
        console.print("[bold red]Invalid input. Please enter a valid number.[/bold red]")
    except KeyboardInterrupt:
        # Gracefully handle Ctrl+C. The 'finally' block will do the cleanup.
        console.print("\n[yellow]Interrupted by user.[/yellow]")
    finally:
        # This block ensures the thread is stopped gracefully, even if an error occurs.
        if volume_thread and volume_thread.is_alive():
            console.print("[bold yellow]Stopping volume lock...[/bold yellow]")
            stop_event.set()
            volume_thread.join()  # Wait for the worker to finish its current loop
            console.print("[bold green]Volume lock stopped.[/bold green]")

        console.print(Panel("[bold green]Goodbye![/bold green]", border_style="green", expand=False))


if __name__ == "__main__":
    main()
