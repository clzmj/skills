class Job {
    val initial = "queued"
    fun ready(state: String) = state == "queued"
    fun reset() = "queued"
}
