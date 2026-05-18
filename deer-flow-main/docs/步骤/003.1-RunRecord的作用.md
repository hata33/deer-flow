# RunRecord 的作用

每次请求调用 `start_run()` 都会创建一个新的 `RunRecord`，包含唯一的 `run_id`。
它的唯一职责是把**这一次 agent 执行**和**对应的 SSE 推流**绑定在一起：agent 按 `run_id` 往 Bridge 里 publish，SSE 消费者按同一个 `run_id` 去 subscribe。
RunRecord 是一次性信封，不跨请求复用，用完即弃——只管"谁发、谁收"。
