// SPDX-License-Identifier: GPL-2.0
import Foundation
import Virtualization
import Darwin

func report(_ text: String) {
    FileHandle.standardError.write(Data((text + "\n").utf8))
}

final class Runner: NSObject, VZVirtualMachineDelegate {
    private var machine: VZVirtualMachine!
    private var timer: Timer?
    private var console: FileHandle!

    func start() throws {
        let args = CommandLine.arguments
        guard args.count >= 10,
              let memoryMB = UInt64(args[5]), memoryMB > 0,
              let cpus = Int(args[6]), cpus > 0,
              let timeout = TimeInterval(args[7]), timeout.isFinite, timeout > 0,
              memoryMB <= UInt64.max / (1024 * 1024) else {
            report("usage: vz-runner Image initramfs disk|- console memoryMB cpus timeout cmdline host-info [ro:/path|rw:/path ...]")
            exit(2)
        }
        guard VZVirtualMachine.isSupported else {
            report("Apple Virtualization Framework is not supported on this host")
            exit(2)
        }
        let config = VZVirtualMachineConfiguration()
        config.platform = VZGenericPlatformConfiguration()
        config.cpuCount = cpus
        config.memorySize = memoryMB * 1024 * 1024
        let loader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: args[1]))
        loader.initialRamdiskURL = URL(fileURLWithPath: args[2])
        loader.commandLine = args[8]
        config.bootLoader = loader
        console = try FileHandle(forWritingTo: URL(fileURLWithPath: args[4]))
        let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
        serial.attachment = VZFileHandleSerialPortAttachment(
            fileHandleForReading: nil, fileHandleForWriting: console)
        config.serialPorts = [serial]
        var disks = args[3] == "-" ? [] : [(path: args[3], readOnly: false)]
        for spec in args.dropFirst(10) {
            guard spec.hasPrefix("ro:/") || spec.hasPrefix("rw:/") else {
                report("invalid block attachment: \(spec)")
                exit(2)
            }
            disks.append((String(spec.dropFirst(3)), spec.hasPrefix("ro:")))
        }
        var identities = Set<String>()
        config.storageDevices = try disks.enumerated().map { index, disk in
            var info = stat()
            guard lstat(disk.path, &info) == 0,
                  (info.st_mode & S_IFMT) == S_IFREG,
                  identities.insert("\(info.st_dev):\(info.st_ino)").inserted else {
                report("expected a distinct regular disk image: \(disk.path)")
                exit(2)
            }
            let attachment = try VZDiskImageStorageDeviceAttachment(
                url: URL(fileURLWithPath: disk.path), readOnly: disk.readOnly)
            let device = VZVirtioBlockDeviceConfiguration(attachment: attachment)
            if index == 1 && !disk.readOnly && args[3] != "-" {
                guard #available(macOS 12.3, *) else {
                    report("the compare data disk requires macOS 12.3 or later")
                    exit(2)
                }
                device.blockDeviceIdentifier = "memcomp-data"
            }
            return device
        }
        config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
        try config.validate()
        try Data(("backend=Apple Virtualization Framework\n" +
                  "memory_mb=\(memoryMB)\ncpus=\(cpus)\n" +
                  "block_devices=\(disks.count)\n" +
                  disks.enumerated().map { i, disk in
                      "block_\(i)=\(disk.readOnly ? "ro" : "rw"):\(disk.path)\n"
                  }.joined() +
                  "cmdline=\(loader.commandLine)\n").utf8)
            .write(to: URL(fileURLWithPath: args[9]))
        machine = VZVirtualMachine(configuration: config)
        machine.delegate = self
        timer = Timer.scheduledTimer(withTimeInterval: timeout, repeats: false) { _ in
            report("VZ timeout after \(timeout) seconds; terminating disposable VM")
            exit(124)
        }
        report("Starting ARM64 guest with Apple Virtualization Framework")
        machine.start { result in
            switch result {
            case .success: report("VZ guest started")
            case .failure(let error):
                report("VZ start failed: \(error)")
                exit(1)
            }
        }
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        timer?.invalidate()
        try? console.synchronize()
        report("VZ guest powered off")
        exit(0)
    }

    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        report("VZ guest failed: \(error)")
        exit(1)
    }
}

let runner = Runner()
do {
    try runner.start()
    RunLoop.main.run()
} catch {
    report("VZ configuration failed: \(error)")
    exit(1)
}
