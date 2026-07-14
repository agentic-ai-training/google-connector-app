import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

class ChatService {
  ChatService({this.baseUrl = 'http://localhost:8000'});
  final String baseUrl;
  Future<String> _token() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('agent_token');
    if (saved != null) return saved;
    final response = await http.post(Uri.parse('$baseUrl/auth/token'), headers: {'Content-Type': 'application/json'}, body: jsonEncode({'email': 'mobile-user@local'}));
    if (response.statusCode >= 400) throw Exception('Authentication failed');
    final token = jsonDecode(response.body)['access_token'] as String;
    await prefs.setString('agent_token', token); return token;
  }
  Stream<String> sendMessage(String message, String sessionId) async* {
    final request = http.Request('POST', Uri.parse('$baseUrl/chat'));
    request.headers.addAll({'Content-Type': 'application/json', 'Authorization': 'Bearer ${await _token()}'});
    request.body = jsonEncode({'message': message, 'session_id': sessionId});
    final response = await http.Client().send(request);
    if (response.statusCode >= 400) throw Exception('Request failed (${response.statusCode})');
    var buffer = '';
    await for (final chunk in response.stream.transform(utf8.decoder)) {
      buffer += chunk; final events = buffer.split('\n\n'); buffer = events.removeLast();
      for (final line in events) { if (line.startsWith('data: ')) { final data = jsonDecode(line.substring(6)); if (data['done'] == false) yield data['token'] as String; } }
    }
  }
  Future<void> sendFeedback(String sessionId, int rating) async {
    final response = await http.post(Uri.parse('$baseUrl/feedback'), headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ${await _token()}'}, body: jsonEncode({'session_id': sessionId, 'rating': rating}));
    if (response.statusCode >= 400) throw Exception('Feedback failed');
  }
}
