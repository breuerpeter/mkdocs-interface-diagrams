# Controller

## MCU
### Interfaces
#### eth0:10.0.0.1/24
#### UART:sensor
### Components
#### app
##### eth0:ctrl
###### Commands

**Telemetry uplink**
1. [[#MCU > eth0:10.0.0.1/24]]
2. [[display#Panel > eth0:10.0.0.2/24]]

##### UART:imu
###### SensorData

**Sensor read**
1. [[#MCU > UART:sensor]]
2. [[sensor#IMU > UART:in]]

## Gateway
### Interfaces
#### eth0:10.0.0.3/24
