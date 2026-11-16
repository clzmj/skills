class Job {
    static final String QUEUED = "queued";
    boolean ready(String state) { return state.equals("queued"); }
    String initial() { return "queued"; }
}
