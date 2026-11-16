class Job {
    const string Queued = "queued";
    bool Ready(string state) { return state == "queued"; }
    string Initial() { return "queued"; }
}
