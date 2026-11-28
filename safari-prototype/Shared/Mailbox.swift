import Darwin
import Foundation

struct ProbeError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

struct Binding: Codable, Equatable {
    let profile: UUID
    let instance: UUID
}

struct Receipt: Codable {
    let sender: Binding
    let status: String
}

enum Transport: String {
    case push, pull
}

struct PullObservation: Codable {
    let status: String
    let wake: String
    let receivedAt: Double
    let alarmScheduledAt: Double?
    let backgroundStartedAt: Double?
}

struct Command: Codable {
    let expires: Double
    let operation: String
    let target: Binding?
    let window: Int?
    let tab: Int?
    var marker: String?
    var readProfile: UUID?
    var receipts: [Receipt] = []
    var pullObservations: [PullObservation] = []
    var claimedBy: Binding?
    // A bounded JSON object, produced only by the bridge after Safari verifies the sender.
    var result: Data?
    var resultReceivedAt: Double?

    var expired: Bool { Date().timeIntervalSince1970 >= expires }
    var claimed: Bool { claimedBy != nil }

    mutating func claim(from sender: Binding, via transport: Transport = .push) -> String {
        guard !expired else { return "expired" }
        if transport == .pull {
            guard window == nil, tab == nil, marker == nil,
                  ((operation == "discover" && target == nil && readProfile == nil) ||
                   (operation == "windows" && target == nil && readProfile != nil) ||
                   (operation == "qualify-window" && target != nil && readProfile == nil)) else {
                return "unsupported_pull_operation"
            }
        } else if readProfile != nil || operation == "qualify-window" || operation == "new-window" {
            return "invalid_push_target"
        }
        let status: String
        if operation == "discover" { status = "discovered" }
        else if (target?.profile ?? readProfile) != sender.profile { status = "wrong_profile" }
        else if let target, target.instance != sender.instance { status = "wrong_instance" }
        else if claimed { status = "already_claimed" }
        else {
            status = "claimed"
            claimedBy = sender
        }
        if receipts.count < 32,
           !receipts.contains(where: { $0.sender == sender && $0.status == status }) {
            receipts.append(Receipt(sender: sender, status: status))
        }
        return status
    }

    func acceptsResult(from sender: Binding, via transport: Transport = .push) -> Bool {
        guard !expired, claimedBy == sender, result == nil else { return false }
        if transport == .pull {
            return (operation == "windows" && target == nil && readProfile == sender.profile) ||
                   (operation == "qualify-window" && target == sender && readProfile == nil)
        }
        return operation != "qualify-window" && operation != "new-window" && target == sender
    }
}

enum Mailbox {
    static let maximum = 24 * 1024
    static let busy = ProbeError("busy")

    private static func privateDirectory(_ url: URL) throws {
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
              info.st_mode & 0o777 == 0o700, info.st_uid == geteuid() else {
            throw ProbeError("group_mailbox_not_private_or_unavailable")
        }
    }

    static func root(_ transport: Transport = .push, create: Bool = false) throws -> URL {
        guard let identifier = Bundle.main.object(forInfoDictionaryKey: "ProbeAppGroup") as? String,
              let group = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier) else {
            throw ProbeError("app_group_not_configured")
        }
        let name = transport == .pull ? "agent-browser-safari-pull-v1" : "agent-browser-safari-probe-v1"
        let root = group.appendingPathComponent(name, isDirectory: true)
        if create, mkdir(root.path, 0o700) != 0, errno != EEXIST {
            throw ProbeError("cannot_create_group_mailbox")
        }
        try privateDirectory(root)
        return root
    }

    // One host at a time publishes a pull request. A killed host releases the flock;
    // the next host uses a new UUID and atomically replaces the stale rendezvous file.
    static func lockPullHost() throws -> Int32 {
        let path = try root(.pull).appendingPathComponent("host.lock").path
        let lock = open(path, O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard lock >= 0 else { throw ProbeError("cannot_open_pull_host_lock") }
        do {
            try lockPrivateFile(lock)
            return lock
        } catch {
            close(lock)
            throw error
        }
    }

    static func publishPull(_ id: UUID) throws -> URL {
        let pending = try root(.pull).appendingPathComponent("pending")
        try Data(id.uuidString.utf8).write(to: pending, options: .atomic)
        return pending
    }

    static func pendingPull() throws -> UUID? {
        let pending = try root(.pull).appendingPathComponent("pending")
        let fd = open(pending.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        if fd < 0 && errno == ENOENT { return nil }
        guard fd >= 0 else { throw ProbeError("invalid_pull_pointer") }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_uid == geteuid(),
              let bytes = try file.read(upToCount: 37), bytes.count == 36,
              let id = UUID(uuidString: String(decoding: bytes, as: UTF8.self)) else {
            throw ProbeError("invalid_pull_pointer")
        }
        return id
    }

    static func create(_ id: UUID, _ command: Command, via transport: Transport = .push) throws -> URL {
        let dir = try root(transport).appendingPathComponent(id.uuidString, isDirectory: true)
        guard mkdir(dir.path, 0o700) == 0 else { throw ProbeError("cannot_create_request") }
        do {
            let fd = open(dir.appendingPathComponent("lock").path,
                          O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw ProbeError("cannot_create_request_lock") }
            close(fd)
            try write(command, in: dir)
            return dir
        } catch {
            try? FileManager.default.removeItem(at: dir)
            throw error
        }
    }

    private static func lockPrivateFile(_ lock: Int32) throws {
        var info = stat()
        guard fstat(lock, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              info.st_mode & 0o777 == 0o600, info.st_uid == geteuid() else {
            throw ProbeError("invalid_request_lock")
        }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
            if errno == EWOULDBLOCK { throw busy }
            throw ProbeError("request_lock_failed")
        }
    }

    private static func write(_ command: Command, in dir: URL) throws {
        let bytes = try JSONEncoder().encode(command)
        guard bytes.count <= maximum else { throw ProbeError("oversized_mailbox") }
        try bytes.write(to: dir.appendingPathComponent("state.json"), options: .atomic)
    }

    // The request directory is private to the current UID and the signed app group (not a
    // defense against the same OS user). Its lock inode is never replaced during the lease.
    static func access<T>(_ id: UUID, via transport: Transport = .push, update: Bool = false,
                          _ body: (inout Command) throws -> T) throws -> T {
        let dir = try root(transport).appendingPathComponent(id.uuidString, isDirectory: true)
        try privateDirectory(dir)
        let lock = open(dir.appendingPathComponent("lock").path, O_RDWR | O_NOFOLLOW | O_NONBLOCK)
        guard lock >= 0 else { throw ProbeError("unknown_request") }
        defer { close(lock) }
        try lockPrivateFile(lock)
        let fd = open(dir.appendingPathComponent("state.json").path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw ProbeError("unknown_request_state") }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_uid == geteuid(),
              let bytes = try file.read(upToCount: maximum + 1), bytes.count <= maximum else {
            throw ProbeError("invalid_request_state")
        }
        var command = try JSONDecoder().decode(Command.self, from: bytes)
        let result = try body(&command)
        if update { try write(command, in: dir) }
        return result
    }
}
