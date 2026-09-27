<?php
// Trusted operator bootstrap, executed through Coolify's Laravel runtime over IAP.
// Never expose this as a model tool. The secret goes to a mode-0600 file, not stdout.
// Copy the file to the operator's ignored secret store, then remove the remote copy.
$email = getenv('GCA_OPERATOR_EMAIL');
if (!$email) {
    throw new RuntimeException('GCA_OPERATOR_EMAIL is required');
}
$user = App\Models\User::where('email', $email)->sole();
$team = $user->teams()->wherePivot('role', 'owner')->sole();
auth()->login($user);
session(['currentTeam' => $team]);
Illuminate\Support\Facades\Gate::authorize('create', App\Models\PersonalAccessToken::class);
$name = 'gca-inventory-read-20260920';
$path = '/tmp/gca-coolify-inventory.env';
if ($user->tokens()->where('name', $name)->exists()) {
    throw new RuntimeException('Token already exists; recover or explicitly rotate it, do not duplicate');
}
if (file_exists($path)) {
    throw new RuntimeException('Credential file already exists; inspect ownership before continuing');
}
umask(0077);
$file = fopen($path, 'x');
if ($file === false) {
    throw new RuntimeException('Cannot create private credential output');
}
try {
    Illuminate\Support\Facades\DB::transaction(function () use ($user, $name, $file, $team) {
        $token = $user->createToken($name, ['read'], now()->addDays(7));
        $value = "COOLIFY_INVENTORY_TOKEN=".$token->plainTextToken."\n";
        if (fwrite($file, $value) !== strlen($value) || !fflush($file)) {
            throw new RuntimeException('Failed to persist token; database transaction will roll back');
        }
        $settings = App\Models\InstanceSettings::get();
        $settings->is_api_enabled = true;
        $settings->save();
        echo json_encode([
            'created' => true, 'token_id' => $token->accessToken->id,
            'team_id' => $team->id, 'abilities' => ['read'],
            'expires_at' => $token->accessToken->expires_at,
            'secret_printed' => false,
        ])."\n";
    });
} catch (Throwable $error) {
    fclose($file);
    unlink($path);
    throw $error;
}
fclose($file);
