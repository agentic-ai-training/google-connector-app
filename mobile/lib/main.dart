import 'package:flutter/material.dart';
import 'screens/chat_screen.dart';
void main()=>runApp(const AgentApp());
class AgentApp extends StatelessWidget {const AgentApp({super.key});@override Widget build(BuildContext context)=>MaterialApp(title:'Google Workspace Agent',theme:ThemeData(colorSchemeSeed:Colors.blue,brightness:Brightness.light),darkTheme:ThemeData(colorSchemeSeed:Colors.blue,brightness:Brightness.dark),themeMode:ThemeMode.system,home:const ChatScreen());}
