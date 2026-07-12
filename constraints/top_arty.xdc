#------------------------------------------------------------------------------
# top_arty.xdc — Digilent Arty A7-100T constraints for top_arty.sv
#
# Pin locations taken from the Digilent Arty A7 master XDC (Rev. E).
#
# NOTE (correction to MANUAL.md §4): the host->FPGA UART data line on the
# Arty is named `uart_txd_in` and sits on pin A9. The name is from the host
# FTDI chip's perspective — the host transmits on it, the FPGA receives.
# `uart_rxd_out` (pin D10) is the FPGA->host direction, used later by the
# result-frame TX layer.
#------------------------------------------------------------------------------

## 100 MHz system clock ---------------------------------------------------------
set_property -dict { PACKAGE_PIN E3  IOSTANDARD LVCMOS33 } [get_ports { clk100 }]
create_clock -add -name sys_clk -period 10.000 -waveform {0 5} [get_ports { clk100 }]

## Reset button (red, active low) -----------------------------------------------
set_property -dict { PACKAGE_PIN C2  IOSTANDARD LVCMOS33 } [get_ports { ck_rst }]

## USB-UART bridge: host -> FPGA data (FPGA receive) ----------------------------
set_property -dict { PACKAGE_PIN A9  IOSTANDARD LVCMOS33 } [get_ports { uart_txd_in }]

## Green LEDs LD4..LD7 -----------------------------------------------------------
set_property -dict { PACKAGE_PIN H5  IOSTANDARD LVCMOS33 } [get_ports { led[0] }]
set_property -dict { PACKAGE_PIN J5  IOSTANDARD LVCMOS33 } [get_ports { led[1] }]
set_property -dict { PACKAGE_PIN T9  IOSTANDARD LVCMOS33 } [get_ports { led[2] }]
set_property -dict { PACKAGE_PIN T10 IOSTANDARD LVCMOS33 } [get_ports { led[3] }]

## Timing exceptions -------------------------------------------------------------
# uart_txd_in and ck_rst are asynchronous inputs; both pass through 2-FF
# synchronizers in RTL, so exclude them from timing analysis.
set_false_path -from [get_ports { uart_txd_in }]
set_false_path -from [get_ports { ck_rst }]

# LEDs are slow human-visible outputs; no meaningful output timing.
set_false_path -to [get_ports { led[*] }]

## Bitstream config (standard Arty settings) -------------------------------------
set_property CFGBVS VCCO        [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]

## USB-UART bridge: FPGA -> host data (FPGA transmit) — TX layer -------------
set_property -dict { PACKAGE_PIN D10 IOSTANDARD LVCMOS33 } [get_ports { uart_rxd_out }]
set_false_path -to [get_ports { uart_rxd_out }]
