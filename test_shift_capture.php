<?php
define('SHIFT_CAPTURE_TEST', true);
require __DIR__ . '/xserver/shift_mentions_handler.php';
function expect($condition, $message) {
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
$first = 'U' . str_repeat('1', 32);
$second = 'U' . str_repeat('2', 32);
$config = ['line_group_id' => 'target-group', 'shift_reminder_capture_names' => [
    'user1' => 'first person', 'user2' => 'second person']];
$event = ['type' => 'message', 'source' => ['type' => 'group', 'groupId' => 'target-group', 'userId' => $first]];
$lookup = function ($id) use ($first) { return $id === $first ? 'first person' : 'second person'; };
$pending = shift_capture_users([$event], $config, $lookup);
expect($pending === ['user1' => $first], 'actual sender was not captured');
$config['shift_reminder_pending_users'] = $pending;
$event['source']['userId'] = $second;
expect(shift_capture_users([$event], $config, $lookup) === ['user1' => $first, 'user2' => $second], 'later sender did not complete capture');
$event['source']['groupId'] = 'other-group';
expect(shift_capture_users([$event], $config, $lookup) === $pending, 'wrong group accepted');
$event['source']['groupId'] = 'target-group';
$event['source']['userId'] = $first;
$event['message'] = ['type' => 'text', 'mention' => ['mentionees' => [['type' => 'user', 'userId' => $second]]]];
expect(shift_capture_users([$event], $config, $lookup) === $pending, 'mention registered another user');
$event['source']['userId'] = 'U' . str_repeat('3', 32);
$ambiguous = shift_capture_users([$event], $config, function ($id) { return 'first person'; });
expect($ambiguous === ['__conflict' => true], 'duplicate name was not flagged for persistent blocking');
expect(shift_signature_valid('{}', base64_encode(hash_hmac('sha256', '{}', 'secret', true)), 'secret'), 'valid signature rejected');
expect(!shift_signature_valid('{"changed":true}', base64_encode(hash_hmac('sha256', '{}', 'secret', true)), 'secret'), 'changed body accepted');
expect(!shift_signature_valid('{}', '', ''), 'missing secret accepted');
echo "Passive sender capture tests passed; no network calls.\n";
