// audiodelay.swift - delays the computer's audio so that it stays in sync with the picture on the TV.
//
// A Core Audio process tap (macOS 14.2+) captures the system audio and mutes it at the source while
// the tap is active. The captured audio is played back on the current output device (speakers or
// headphones) through a private aggregate device, delayed by a configurable amount. When the
// process exits the tap is destroyed and macOS restores the original audio.
//
// Commands on stdin, one per line:  delay SECONDS | quit
// Usage: audiodelay [--delay SECONDS]

import CoreAudio
import Foundation

func note(_ msg: String) {
    FileHandle.standardError.write("audiodelay: \(msg)\n".data(using: .utf8)!)
}

func fail(_ msg: String) -> Never {
    note(msg)
    exit(1)
}

var initialDelay = 0.0
var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    if a == "--delay", !args.isEmpty, let v = Double(args.removeFirst()) {
        initialDelay = v
    } else {
        fail("unknown argument \(a)")
    }
}

// MARK: - Core Audio helpers

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func defaultOutputDevice() -> AudioObjectID {
    var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
    var id = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id)
    return id
}

func deviceUID(_ id: AudioObjectID) -> String? {
    var addr = address(kAudioDevicePropertyDeviceUID)
    var uid: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &uid) == noErr, let u = uid else { return nil }
    return u.takeRetainedValue() as String
}

func ownProcessObject() -> AudioObjectID {
    var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var pid = getpid()
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                               UInt32(MemoryLayout<pid_t>.size), &pid, &size, &obj)
    return obj
}

// MARK: - Delay line

/// Stereo ring buffer. The I/O thread writes the captured audio and reads it back `delay` frames
/// later. Delay changes under 100 ms are reached by playing 5% slower or faster (inaudible), larger
/// ones by jumping.
final class DelayLine {
    let capacity: Int
    var left: [Float], right: [Float]
    var writePos = 0
    var readPos = 0.0
    var sampleRate: Double
    var target = 0.0          // seconds; written by the command thread
    var peak: Float = 0

    init(sampleRate: Double, maxSeconds: Double = 4) {
        self.sampleRate = sampleRate
        capacity = Int(sampleRate * maxSeconds)
        left = [Float](repeating: 0, count: capacity)
        right = [Float](repeating: 0, count: capacity)
    }

    func write(_ l: Float, _ r: Float) {
        left[writePos % capacity] = l
        right[writePos % capacity] = r
        writePos += 1
        peak = max(peak, abs(l), abs(r))
    }

    func read() -> (Float, Float) {
        let want = Double(writePos) - min(target, Double(capacity) / sampleRate - 0.1) * sampleRate
        let diff = want - readPos
        if abs(diff) > 0.1 * sampleRate {
            readPos = want                                   // big change: jump
        } else {
            readPos += diff > 1 ? 1.05 : (diff < -1 ? 0.95 : 1)
        }
        if readPos < Double(writePos - capacity + 1) || readPos > Double(writePos - 1) {
            return (0, 0)
        }
        let i = Int(readPos), f = Float(readPos - Double(i))
        let a = i % capacity, b = (i + 1) % capacity
        return (left[a] + (left[b] - left[a]) * f, right[a] + (right[b] - right[a]) * f)
    }
}

// MARK: - Tap and aggregate device

final class AudioDelay {
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?
    var tapUUID = UUID()
    var outputDevice = AudioObjectID(kAudioObjectUnknown)
    var line: DelayLine?

    func createTap() {
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [ownProcessObject()])
        desc.uuid = tapUUID
        desc.name = "Device Cast audio delay"
        desc.isPrivate = true
        desc.muteBehavior = .mutedWhenTapped
        let err = AudioHardwareCreateProcessTap(desc, &tapID)
        if err != noErr { fail("cannot create the audio tap (\(err)); System Audio Recording permission missing?") }
    }

    func tapSampleRate() -> Double {
        var addr = address(kAudioTapPropertyFormat)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd) == noErr, asbd.mSampleRate > 0 else { return 48000 }
        return asbd.mSampleRate
    }

    /// Aggregate device = current output device + the tap. Its I/O callback reads the tapped audio
    /// and writes it, delayed, to the output.
    func start() {
        outputDevice = defaultOutputDevice()
        guard let outUID = deviceUID(outputDevice) else { fail("no audio output device") }
        let desc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Device Cast delay",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: tapUUID.uuidString]],
        ]
        var err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggregateID)
        if err != noErr { fail("cannot create the aggregate device (\(err))") }

        let rate = tapSampleRate()
        let previous = line
        let dl = DelayLine(sampleRate: rate)
        dl.target = previous?.target ?? initialDelay
        line = dl

        err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inData, _, outData, _ in
            let input = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
            let output = UnsafeMutableAudioBufferListPointer(outData)
            // Input: the tap (interleaved or one buffer per channel).
            if let first = input.first, let base = first.mData {
                let ch = max(1, Int(first.mNumberChannels))
                let frames = Int(first.mDataByteSize) / (4 * ch)
                let p = base.assumingMemoryBound(to: Float.self)
                let second = input.count > 1 ? input[1].mData?.assumingMemoryBound(to: Float.self) : nil
                for i in 0..<frames {
                    let l = p[i * ch]
                    let r = ch > 1 ? p[i * ch + 1] : (second?[i] ?? l)
                    dl.write(l, r)
                }
            }
            // Output: every buffer of the output device, interleaved or not.
            guard let firstOut = output.first else { return }
            let outCh = max(1, Int(firstOut.mNumberChannels))
            let frames = Int(firstOut.mDataByteSize) / (4 * outCh)
            for i in 0..<frames {
                let (l, r) = dl.read()
                var c = 0
                for buf in output {
                    guard let base = buf.mData else { continue }
                    let n = max(1, Int(buf.mNumberChannels))
                    let p = base.assumingMemoryBound(to: Float.self)
                    for k in 0..<n {
                        p[i * n + k] = (c + k) % 2 == 0 ? l : r
                    }
                    c += n
                }
            }
        }
        if err != noErr { fail("cannot create the I/O callback (\(err))") }
        err = AudioDeviceStart(aggregateID, procID)
        if err != noErr { fail("cannot start the audio device (\(err))") }
        note("active on \(outUID), \(Int(rate)) Hz, delay \(Int(dl.target * 1000)) ms")
    }

    func stop() {
        if let p = procID {
            AudioDeviceStop(aggregateID, p)
            AudioDeviceDestroyIOProcID(aggregateID, p)
            procID = nil
        }
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    func shutdown() {
        stop()
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }
}

// MARK: - Main

let delay = AudioDelay()
delay.createTap()
delay.start()

for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { delay.shutdown(); exit(0) }
    src.resume()
    objc_setAssociatedObject(delay, "sig\(sig)", src, .OBJC_ASSOCIATION_RETAIN)
}

// Follow the default output device (e.g. headphones plugged in) and report every 10 s.
var ticks = 0
let monitor = DispatchSource.makeTimerSource(queue: .main)
monitor.schedule(deadline: .now() + 1, repeating: 1)
monitor.setEventHandler {
    if defaultOutputDevice() != delay.outputDevice {
        note("output device changed")
        delay.stop()
        delay.start()
    }
    ticks += 1
    if ticks % 10 == 0, let dl = delay.line {
        note(String(format: "delay %.0f ms, peak %.2f", dl.target * 1000, dl.peak))
        dl.peak = 0
    }
}
monitor.resume()

Thread {
    while let line = readLine() {
        let parts = line.split(separator: " ")
        if parts.first == "quit" { break }
        if parts.count == 2, parts[0] == "delay", let v = Double(parts[1]) {
            delay.line?.target = max(0, min(3, v))
        }
    }
    DispatchQueue.main.async { delay.shutdown(); exit(0) }
}.start()

dispatchMain()
