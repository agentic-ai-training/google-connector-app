<?php
// Operator-only bootstrap inside the already-authenticated Coolify Laravel runtime.
// This is not a model tool. Plaintext tokens go only to private, one-time files.

$email = getenv('GCA_OPERATOR_EMAIL');
if (!$email) {
    throw new RuntimeException('GCA_OPERATOR_EMAIL is required');
}

$user = App\Models\User::where('email', $email)->sole();
$team = $user->teams()->wherePivot('role', 'owner')->sole();
auth()->login($user);
session(['currentTeam' => $team]);
Illuminate\Support\Facades\Gate::authorize('create', App\Models\PersonalAccessToken::class);

$expiresAt = Carbon\CarbonImmutable::parse('2026-10-19T17:24:32Z');
if ($expiresAt->isPast()) {
    throw new RuntimeException('Fixed host term has ended; token renewal needs new authority');
}

$roles = [
    'inventory' => ['abilities' => ['read'], 'variable' => 'COOLIFY_INVENTORY_TOKEN'],
    'configuration' => ['abilities' => ['read', 'write'],
                        'variable' => 'COOLIFY_CONFIGURATION_TOKEN'],
    'deployment' => ['abilities' => ['deploy'], 'variable' => 'COOLIFY_DEPLOYMENT_TOKEN'],
    'recovery' => ['abilities' => ['read', 'read:sensitive'],
                   'variable' => 'COOLIFY_RECOVERY_TOKEN'],
];

$handles = [];
$paths = [];
$created = [];
umask(0077);
try {
    foreach ($roles as $role => $policy) {
        $name = 'gca-' . $role . '-deployment-v1';
        $path = '/tmp/gca-coolify-' . $role . '-deployment-v1.env';
        if ($user->tokens()->where('name', $name)->exists() || file_exists($path)
            || is_link($path)) {
            throw new RuntimeException('Existing token or credential file; rotate explicitly');
        }
        $handle = fopen($path, 'x');
        if ($handle === false) {
            throw new RuntimeException('Could not reserve private credential output');
        }
        $handles[$role] = $handle;
        $paths[$role] = $path;
    }

    Illuminate\Support\Facades\DB::transaction(function () use (
        $user, $team, $roles, $expiresAt, $handles, &$created
    ) {
        foreach ($roles as $role => $policy) {
            $name = 'gca-' . $role . '-deployment-v1';
            $issued = $user->createToken($name, $policy['abilities'], $expiresAt);
            $plain = $issued->plainTextToken;
            if (!preg_match('/^[A-Za-z0-9|_-]{1,512}$/', $plain)) {
                throw new RuntimeException('Unexpected token encoding');
            }
            $content = $policy['variable'] . "='" . $plain . "'\n";
            if (fwrite($handles[$role], $content) !== strlen($content)
                || !fflush($handles[$role])) {
                throw new RuntimeException('Credential persistence failed; rolling back');
            }
            $created[$role] = [
                'token_id' => $issued->accessToken->id,
                'team_id' => $team->id,
                'abilities' => $policy['abilities'],
                'expires_at' => $issued->accessToken->expires_at,
            ];
        }
    });
} catch (Throwable $error) {
    foreach ($handles as $handle) {
        fclose($handle);
    }
    foreach ($paths as $path) {
        unlink($path);
    }
    throw $error;
}
foreach ($handles as $handle) {
    fclose($handle);
}
echo json_encode(['created' => $created, 'secret_printed' => false]) . "\n";
