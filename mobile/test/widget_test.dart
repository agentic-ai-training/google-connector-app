import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/main.dart';
void main() {
  testWidgets('Agent app renders', (tester) async {
    await tester.pumpWidget(const AgentApp());
    await tester.pump();
    expect(find.text('Workspace Agent'), findsOneWidget);
  });
}
