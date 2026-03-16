# Systemd Setup (Linux)

## Installation

1. Copy and customize the service template:
```bash
sudo cp systemd/kiro-gateway.service.template /etc/systemd/system/kiro-gateway.service
```

2. Edit the file and replace paths:
```bash
sudo sed -i 's|/path/to/kiro-gateway|/actual/path/to/kiro-gateway|g' /etc/systemd/system/kiro-gateway.service
```

3. Reload systemd and enable on boot:
```bash
sudo systemctl daemon-reload
sudo systemctl enable kiro-gateway
```

## Usage

```bash
# Start service
sudo systemctl start kiro-gateway

# Stop service
sudo systemctl stop kiro-gateway

# Check status
sudo systemctl status kiro-gateway

# View logs
tail -f log/gateway.log
# or
sudo journalctl -u kiro-gateway -f
```

## Notes

- The service runs on port 8001 by default. Edit `/etc/systemd/system/kiro-gateway.service` to change it.
- Logs are written to `log/gateway.log` and also available via journalctl.
- The service will auto-restart on failure with 5-second delay.
