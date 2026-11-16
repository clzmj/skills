import Foundation

enum JobState: String, Codable {
    case queued = "queued"
    case running = "running"
    case failed = "failed"
}

struct Job: Codable {
    let id: String
    let state: JobState
}

func wireState(_ job: Job) -> String {
    switch job.state {
    case .queued: return "queued"
    case .running: return "running"
    case .failed: return "failed"
    }
}

func isQueued(_ value: String) -> Bool {
    return value == "queued"
}
