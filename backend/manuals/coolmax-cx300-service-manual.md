# CoolMax CX-300 Air-Cooled Chiller — Field Service Manual (excerpt)

## Safety before any work
Switch the unit to LOCAL STOP and isolate the main breaker before opening the electrical panel. Capacitors can hold charge: discharge with an insulated 20 kOhm resistor before touching terminals. Wear insulated gloves. Never bypass the high pressure switch.

## Error code table
- E1: Low water flow. Check strainer and chilled water pump.
- E2: Low suction pressure. Possible refrigerant leak or iced evaporator.
- E3: Compressor overload. Check supply voltage and compressor amps.
- E4: High discharge pressure trip. The high pressure switch opened because heat is not being rejected at the condenser. Most common causes, in order: (1) failing condenser fan run capacitor, (2) dirty or blocked condenser coil, (3) condenser fan motor failure, (4) faulty high pressure switch, (5) refrigerant overcharge. E4 that appears mainly in hot afternoons strongly suggests a weak fan capacitor or a dirty coil.
- E5: Sensor fault, discharge temperature probe open circuit.
- E6: Phase failure or phase reversal.

## E4 troubleshooting procedure (high discharge pressure)
Step 1. Check the condenser fan. With the unit running, confirm all condenser fans spin at full speed. A fan that starts slowly, hums, or stops when hot points to the run capacitor or the fan motor.
Step 2. Inspect the condenser coil. Look for dust, leaves or plastic covering the fins. If more than 30 percent of the coil is blocked, clean it with a coil brush and low pressure water from inside out.
Step 3. Test the fan run capacitor. Isolate power and discharge the capacitor. Measure capacitance with a meter. Rated value for CX-300 is 45/5 uF (part CAP-45). Replace if the reading is more than 10 percent below rating, i.e. below 40.5 uF, or if the capacitor is bulging or leaking.
Step 4. Check the fan motor. If the capacitor is good, measure motor winding resistance. Open winding or seized bearing means replace motor FAN-CX.
Step 5. Check the high pressure switch. With gauges fitted, the switch must open at 28 bar and reset at 22 bar. Replace switch PSW-HP if it opens below 26 bar.
Step 6. Reset. Press RESET on the controller and run for 15 minutes. Discharge pressure should stay between 16 and 22 bar at 35 C ambient.

## Temporary measure while waiting for parts
If the capacitor is weak and no spare is available, the unit can be run with reduced load: set the capacity limit to 60 percent on the controller (Menu > Limits > Capacity). Inform the customer that cooling capacity is reduced. Do not leave the unit running with a bypassed high pressure switch.

## Spare parts list
- CAP-45: Condenser fan run capacitor 45/5 uF, 440 V.
- FAN-CX: Condenser fan motor, 1/2 HP, 6 pole.
- PSW-HP: High pressure switch, cut-out 28 bar.

## Service intervals
Quarterly: clean condenser coil, check fan amps, check capacitor value. Yearly: refrigerant leak test, tighten electrical terminals, calibrate sensors.
