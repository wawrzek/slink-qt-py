#!/usr/bin/env python3
"""
Scratch Link implementation using PyQt6
Supports both Bluetooth Classic (for EV3) and BLE devices
Uses unencrypted WebSocket (WS) instead of WSS
"""

import sys
import signal
import json
from PyQt6.QtCore import QObject, pyqtSlot, QTimer
from PyQt6.QtWebSockets import QWebSocketServer, QWebSocket
from PyQt6.QtBluetooth import (
    QBluetoothDeviceDiscoveryAgent,
    QBluetoothDeviceInfo,
    QBluetoothSocket,
    QBluetoothAddress,
    QBluetoothServiceInfo,
    QBluetoothServiceDiscoveryAgent,
    QBluetoothUuid,
    QLowEnergyController
)
from PyQt6.QtWidgets import QApplication


class ScratchLinkServer(QObject):
    """Main server handling WebSocket connections from Scratch"""

    def __init__(self, port, mode='BT'):
        super().__init__()
        self.port = port
        self.mode = mode  # 'BT' for classic, 'BLE' for low energy
        self.clients = []

        # Setup WebSocket server (NonSecureMode for WS instead of WSS)
        self.server = QWebSocketServer(
            f"Scratch Link {mode}",
            QWebSocketServer.SslMode.NonSecureMode
        )

        # Bluetooth components
        self.bt_discovery = QBluetoothDeviceDiscoveryAgent()
        self.bt_discovery.deviceDiscovered.connect(self.on_device_discovered)
        self.bt_discovery.finished.connect(self.on_discovery_finished)

        self.discovered_devices = {}  # Store discovered devices by address
        self.bt_socket = None  # For classic Bluetooth
        self.ble_controller = None  # For BLE
        self.current_client = None  # Track which client is using BT
        self.service_discovery = None  # For service discovery
        self.pending_connect_data = None  # Store connect request data

        # Start server
        if self.server.listen(port=self.port):
            print(f"Scratch Link {mode} server listening on WS port {self.port}")
            self.server.newConnection.connect(self.on_new_connection)
        else:
            print(f"Failed to start server on port {self.port}")

    @pyqtSlot()
    def on_new_connection(self):
        """Handle new WebSocket connection from Scratch"""
        client = self.server.nextPendingConnection()
        if client:
            print(f"New client connected: {client.peerAddress().toString()}")
            client.textMessageReceived.connect(self.on_message_received)
            client.disconnected.connect(lambda: self.on_client_disconnected(client))
            self.clients.append(client)

    @pyqtSlot(str)
    def on_message_received(self, message):
        """Handle messages from Scratch"""
        client = self.sender()
        print(f"Received: {message}")

        try:
            data = json.loads(message)
            method = data.get('method')

            # Handle different Scratch Link protocol methods
            if method == 'discover':
                self.handle_discover(client, data)
            elif method == 'connect':
                self.handle_connect(client, data)
            elif method == 'send':
                self.handle_send(client, data)
            elif method == 'read':
                self.handle_read(client, data)
            else:
                self.send_error(client, f"Unknown method: {method}")

        except json.JSONDecodeError:
            self.send_error(client, "Invalid JSON")

    def handle_discover(self, client, data):
        """Start Bluetooth device discovery"""
        print(f"Starting {self.mode} discovery...")
        self.current_client = client
        self.bt_discovery.start()

        # Send acknowledgment
        response = {
            'jsonrpc': '2.0',
            'id': data.get('id'),
            'result': None
        }
        client.sendTextMessage(json.dumps(response))

    def handle_connect(self, client, data):
        """Connect to a specific Bluetooth device"""
        params = data.get('params', {})
        peripheral_id = params.get('peripheralId')

        print(f"Connecting to device: {peripheral_id}")
        self.current_client = client

        if self.mode == 'BT':
            # Classic Bluetooth connection (default is RFCOMM)
            self.bt_socket = QBluetoothSocket()
            self.bt_socket.connected.connect(lambda: self.on_bt_connected(client, data))
            self.bt_socket.errorOccurred.connect(lambda err: self.on_bt_error(client, err))
            self.bt_socket.readyRead.connect(lambda: self.on_bt_data_ready(client))
            address = QBluetoothAddress(peripheral_id)
            # Use Serial Port Profile UUID for EV3 (required by BlueZ on Linux)
            # SPP UUID: 00001101-0000-1000-8000-00805F9B34FB
            spp_uuid = QBluetoothUuid("00001101-0000-1000-8000-00805F9B34FB")
            self.bt_socket.connectToService(address, spp_uuid)
            print(f"Connecting to {peripheral_id} using SPP UUID...")
        else:
            # BLE connection - need QBluetoothDeviceInfo, not just address
            if peripheral_id in self.discovered_devices:
                device_info = self.discovered_devices[peripheral_id]
                # Keep a reference to prevent garbage collection
                self.ble_controller = QLowEnergyController.createCentral(device_info, self)
                self.ble_controller.connected.connect(lambda: self.on_ble_connected(client, data))
                self.ble_controller.errorOccurred.connect(lambda err: self.on_ble_error(client, err))
                self.ble_controller.connectToDevice()
            else:
                self.send_error(client, f"Device {peripheral_id} not found. Please discover devices first.")

    def handle_send(self, client, data):
        """Send data to connected Bluetooth device"""
        params = data.get('params', {})
        message = params.get('message')
        encoding = params.get('encoding', 'base64')

        # Convert message based on encoding
        if encoding == 'base64':
            import base64
            payload = base64.b64decode(message)
        else:
            payload = message.encode()

        print(f"Payload hex: {payload.hex()}")

        if self.mode == 'BT' and self.bt_socket:
            if self.bt_socket.state() == QBluetoothSocket.SocketState.ConnectedState:
                bytes_written = self.bt_socket.write(payload)
                self.bt_socket.flush()  # Force send immediately
                print(f"Sent {bytes_written} bytes to EV3 (state: {self.bt_socket.state()})")
                response = {
                    'jsonrpc': '2.0',
                    'id': data.get('id'),
                    'result': bytes_written
                }
                client.sendTextMessage(json.dumps(response))
            else:
                print(f"Socket state: {self.bt_socket.state()}")
                self.send_error(client, "Bluetooth socket not connected")
        else:
            self.send_error(client, "No Bluetooth connection available")

    def handle_read(self, client, data):
        """Read data from connected Bluetooth device"""
        if self.mode == 'BT' and self.bt_socket:
            available = self.bt_socket.bytesAvailable()
            if available > 0:
                data_bytes = self.bt_socket.read(available)
                import base64
                encoded = base64.b64encode(data_bytes).decode()

                response = {
                    'jsonrpc': '2.0',
                    'method': 'didReceiveMessage',
                    'params': {
                        'message': encoded,
                        'encoding': 'base64'
                    }
                }
                client.sendTextMessage(json.dumps(response))

    @pyqtSlot(QBluetoothDeviceInfo)
    def on_device_discovered(self, device):
        """Handle discovered Bluetooth device"""
        device_address = device.address().toString()
        print(f"Found device: {device.name()} - {device_address}")
        # Store a copy of the device info for later connection
        # This prevents garbage collection issues
        device_copy = QBluetoothDeviceInfo(device)
        self.discovered_devices[device_address] = device_copy
        # Send device info to Scratch
        if hasattr(self, 'current_client'):
            response = {
                'jsonrpc': '2.0',
                'method': 'didDiscoverPeripheral',
                'params': {
                    'peripheralId': device_address,
                    'name': device.name(),
                    'rssi': device.rssi()
                }
            }
            self.current_client.sendTextMessage(json.dumps(response))

    @pyqtSlot()
    def on_discovery_finished(self):
        """Discovery scan completed"""
        print("Discovery finished")

    def on_service_discovered(self, service):
        """Handle discovered Bluetooth service"""
        print(f"Found service: {service.serviceName()} - {service.serviceUuid().toString()}")
        # Connect to the first SPP service found
        if service.serviceUuid() == QBluetoothUuid("00001101-0000-1000-8000-00805F9B34FB"):
            print("Found SPP service, connecting...")
            self.bt_socket.connectToService(service)
            self.service_discovery.stop()

    def on_service_discovery_finished(self):
        """Service discovery completed"""
        print("Service discovery finished")
        if self.bt_socket and self.bt_socket.state() != QBluetoothSocket.SocketState.ConnectedState:
            # No SPP service found or connection failed
            if self.pending_connect_data:
                client, data = self.pending_connect_data
                self.send_error(client, "Could not find SPP service on device")
                self.pending_connect_data = None

    def on_bt_connected(self, client, data):
        """Classic Bluetooth connection established"""
        print(f"Bluetooth connected! Socket state: {self.bt_socket.state()}")
        print(f"Socket is writable: {self.bt_socket.isWritable()}")
        print(f"Socket is readable: {self.bt_socket.isReadable()}")
        response = {
            'jsonrpc': '2.0',
            'id': data.get('id'),
            'result': None
        }
        client.sendTextMessage(json.dumps(response))

    def on_bt_data_ready(self, client):
        """Handle incoming data from Bluetooth device"""
        if self.bt_socket and self.bt_socket.bytesAvailable() > 0:
            data_bytes = self.bt_socket.readAll()
            import base64
            encoded = base64.b64encode(bytes(data_bytes)).decode()
            print(f"Received {len(data_bytes)} bytes from EV3")
            response = {
                'jsonrpc': '2.0',
                'method': 'didReceiveMessage',
                'params': {
                    'message': encoded,
                    'encoding': 'base64'
                }
            }
            client.sendTextMessage(json.dumps(response))

    def on_bt_error(self, client, error):
        """Handle Bluetooth connection error"""
        error_string = self.bt_socket.errorString() if self.bt_socket else "Unknown error"
        print(f"Bluetooth error: {error} - {error_string}")
        self.send_error(client, f"Bluetooth error: {error_string}")

    def on_ble_connected(self, client, data):
        """BLE connection established"""
        print("BLE connected!")
        response = {
            'jsonrpc': '2.0',
            'id': data.get('id'),
            'result': None
        }
        client.sendTextMessage(json.dumps(response))

    def on_ble_error(self, client, error):
        """Handle BLE connection error"""
        print(f"BLE error: {error}")
        self.send_error(client, f"BLE error: {error}")

    def on_client_disconnected(self, client):
        """Handle client disconnection"""
        print("Client disconnected")
        if client in self.clients:
            self.clients.remove(client)

    def send_error(self, client, message):
        """Send error message to client"""
        response = {
            'jsonrpc': '2.0',
            'error': {
                'message': message
            }
        }
        client.sendTextMessage(json.dumps(response))


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    print("\nCtrl+C detected, shutting down...")
    QApplication.quit()


def main():
    app = QApplication(sys.argv)

    # Set up signal handler
    signal.signal(signal.SIGINT, signal_handler)

    # Create a timer to allow Python to process signals
    # The timer runs every 500ms and does nothing, but allows
    # the Python interpreter to handle signals
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(500)

    # Start both BT and BLE servers on WS (unencrypted)
    bt_server = ScratchLinkServer(20111, 'BT')   # Classic Bluetooth
    #bt_server = ScratchLinkServer(20110, 'BT')   # Classic Bluetooth
    #ble_server = ScratchLinkServer(20111, 'BLE')  # Bluetooth Low Energy


    print("\nScratch Link servers started (WS mode - unencrypted)!")
    print("Connect from Scratch using ws://localhost:20110 and ws://localhost:20111")

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
