// SPDX-License-Identifier: GPL-2.0
import Darwin
import Foundation
import Virtualization

func fail(_ message: String, status: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(status)
}

// The wrapper starts us in a new private directory. Never open an existing disk.
func scratchDisk() throws -> VZVirtioBlockDeviceConfiguration {
    let fd = open("swap.raw", O_CREAT | O_EXCL | O_RDWR, 0o600)
    guard fd >= 0 else { fail("create swap.raw: \(String(cString: strerror(errno)))") }
    defer { close(fd) }
    guard ftruncate(fd, 2 * 1024 * 1024 * 1024) == 0 else {
        fail("size swap.raw: \(String(cString: strerror(errno)))")
    }
    let disk = try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: "swap.raw"),
                                                      readOnly: false)
    return VZVirtioBlockDeviceConfiguration(attachment: disk)
}

final class Delegate: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ virtualMachine: VZVirtualMachine) { exit(0) }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        fail("VZ stopped: \(error)")
    }
}

let args = CommandLine.arguments
guard args.count == 3 else { fail("usage: vz-runner Image initramfs.gz", status: 2) }
guard VZVirtualMachine.isSupported else { fail("virtualization is unavailable") }
let loader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: args[1]))
loader.initialRamdiskURL = URL(fileURLWithPath: args[2])
loader.commandLine = "console=hvc0 rdinit=/init panic=-1 RUN_SELFTEST=0 " +
    "FILL_MB=1792 PRESS_MB=64 CHUNK_MB=4 PAGEOUT_LAG=0 PACE_US=3000 THP=never " +
    "WB_WAIT_S=5 TEST_TIMEOUT=120 HEARTBEAT_S=1 SKIP_CHURN=1 SKIP_RETAIN=1 " +
    "SKIP_UFFD=1 SKIP_COMPAT=1 SKIP_CYCLES=1 SKIP_CONC=1 SKIP_SWAPOFF=1"

let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: nil,
                                                     fileHandleForWriting: .standardOutput)
let config = VZVirtualMachineConfiguration()
config.bootLoader = loader
config.cpuCount = 4
config.memorySize = 1024 * 1024 * 1024
config.serialPorts = [serial]
config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
do {
    config.storageDevices = [try scratchDisk()]
    try config.validate()
} catch {
    fail("VZ configuration: \(error)")
}

let delegate = Delegate()
let machine = VZVirtualMachine(configuration: config)
machine.delegate = delegate
DispatchQueue.main.asyncAfter(deadline: .now() + 180) { fail("VZ timeout", status: 124) }
machine.start { result in
    if case .failure(let error) = result { fail("VZ start: \(error)") }
}
RunLoop.main.run()
