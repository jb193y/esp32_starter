# boot.py (Valve Controller)
import gc
import sys
import machine
import time

print(">>> VALVE boot.py STARTED <<<")
print(" Safe boot delay... press Ctrl-C to enter REPL")
time.sleep(4) # delay for serial connection stabilizer and thread prevention

print(" Platform:", sys.platform)
print(" CPU Frequency:", machine.freq() // 1000000, "MHz")
gc.collect()
alloc = gc.mem_alloc()
free = gc.mem_free()
print(" Total Memory:", alloc + free, "bytes")
print(" Free Memory:", free, "bytes")

print(">>> VALVE boot.py COMPLETED <<<")
print("aryan")
import main
