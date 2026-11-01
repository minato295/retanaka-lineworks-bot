<?php
declare(strict_types=1);

function shift_signature_valid(string $body, string $signature, string $secret): bool {
    return $secret !== '' && hash_equals(base64_encode(hash_hmac('sha256', $body, $secret, true)), $signature);
}

function shift_capture_users(array $events, array $config, callable $profile): array {
    $found = $config['shift_reminder_pending_users'] ?? [];
    foreach (array_slice($events, 0, 10) as $event) {
        if (($event['type'] ?? '') !== 'message'
            || ($event['source']['type'] ?? '') !== 'group'
            || ($event['source']['groupId'] ?? '') !== ($config['line_group_id'] ?? '')) {
            continue;
        }
        $targets = $config['shift_reminder_capture_names'] ?? [];
        $id = $event['source']['userId'] ?? '';
        if (!preg_match('/^U[0-9a-fA-F]{32}$/D', $id)) {
            continue;
        }
        $name = $profile($id);
        foreach ($targets as $key => $target) {
            if ($name === $target) {
                if (isset($found[$key]) && $found[$key] !== $id) {
                    return ['__conflict' => true];
                }
                $found[$key] = $id;
            }
        }
    }
    return $found;
}

if (defined('SHIFT_CAPTURE_TEST')) {
    return;
}

ini_set('display_errors', '0');
header('Content-Type: application/json');
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405);
    exit('{"ok":false}');
}
$body = file_get_contents('php://input', false, null, 0, 262145);
if ($body === false || strlen($body) > 262144) {
    http_response_code(413);
    exit('{"ok":false}');
}
$configPath = __DIR__ . '/config.json';
$lock = null;
try {
    $config = json_decode(file_get_contents($configPath), true, 32, JSON_THROW_ON_ERROR);
    if (!shift_signature_valid($body, $_SERVER['HTTP_X_LINE_SIGNATURE'] ?? '', $config['line_channel_secret'] ?? '')) {
        http_response_code(403);
        exit('{"ok":false}');
    }
    if (($config['shift_reminder_capture_enabled'] ?? false) !== true) {
        exit('{"ok":true}');
    }
    $request = json_decode($body, true, 32, JSON_THROW_ON_ERROR);
    $events = $request['events'] ?? [];
    if (!is_array($events)) {
        throw new RuntimeException('invalid events');
    }
    $lookup = function (string $id) use ($config): string {
        $handle = curl_init('https://api.line.me/v2/bot/group/' . rawurlencode($config['line_group_id']) . '/member/' . rawurlencode($id));
        curl_setopt_array($handle, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 5,
            CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $config['line_channel_access_token']]]);
        $response = curl_exec($handle);
        $status = curl_getinfo($handle, CURLINFO_HTTP_CODE);
        curl_close($handle);
        if ($status !== 200 || $response === false) {
            throw new RuntimeException('profile lookup failed');
        }
        $profile = json_decode($response, true, 32, JSON_THROW_ON_ERROR);
        return $profile['displayName'] ?? '';
    };
    if ($events) {
        $lock = fopen($config['lock_file'], 'c');
        if ($lock === false || !flock($lock, LOCK_EX)) {
            throw new RuntimeException('lock unavailable');
        }
        $config = json_decode(file_get_contents($configPath), true, 32, JSON_THROW_ON_ERROR);
        if (($config['shift_reminder_capture_enabled'] ?? false) === true) {
            $users = shift_capture_users($events, $config, $lookup);
            if (!$users) {
                echo '{"ok":true}';
                return;
            }
            if (isset($users['__conflict'])) {
                $config['shift_reminder_capture_conflict'] = true;
                $config['shift_reminder_capture_enabled'] = false;
            } else {
                $config['shift_reminder_pending_users'] = $users;
            }
            if (empty($config['shift_reminder_capture_conflict']) && isset($users['user1'], $users['user2']) && $users['user1'] !== $users['user2']) {
                $config['shift_reminder'] = ['enabled' => true, 'users' => $users];
                $config['shift_reminder_capture_enabled'] = false;
                unset($config['shift_reminder_capture_names'], $config['shift_reminder_pending_users']);
            }
            $temporary = tempnam(__DIR__, '.shift-config-');
            if ($temporary === false) {
                throw new RuntimeException('temporary file unavailable');
            }
            chmod($temporary, 0600);
            $encoded = json_encode($config, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . "\n";
            if (file_put_contents($temporary, $encoded) !== strlen($encoded) || !rename($temporary, $configPath)) {
                @unlink($temporary);
                throw new RuntimeException('config update failed');
            }
        }
    }
    echo '{"ok":true}';
} catch (Throwable $error) {
    http_response_code(500);
    echo '{"ok":false}';
} finally {
    if (is_resource($lock)) {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
}
