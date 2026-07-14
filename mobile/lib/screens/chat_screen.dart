import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';
import '../services/chat_service.dart';

class ChatMessage { ChatMessage(this.user, this.text); final bool user; String text; }
class ChatScreen extends StatefulWidget { const ChatScreen({super.key}); @override State<ChatScreen> createState()=>_ChatScreenState(); }
class _ChatScreenState extends State<ChatScreen> {
  final messages=<ChatMessage>[]; final input=TextEditingController(); final service=ChatService(); String session=''; bool streaming=false; String? error;
  @override void initState(){super.initState();_session();}
  Future<void> _session() async {final prefs=await SharedPreferences.getInstance();var id=prefs.getString('session_id');if(id==null){id=const Uuid().v4();await prefs.setString('session_id',id);}if(mounted)setState(()=>session=id!);}
  Future<void> send() async {final text=input.text.trim();if(text.isEmpty||streaming)return;input.clear();setState((){messages.add(ChatMessage(true,text));messages.add(ChatMessage(false,''));streaming=true;error=null;});try{await for(final token in service.sendMessage(text,session)){if(mounted)setState(()=>messages.last.text+=token);}}catch(e){if(mounted)setState(()=>error=e.toString());}finally{if(mounted)setState(()=>streaming=false);}}
  @override Widget build(BuildContext context)=>Scaffold(appBar:AppBar(title:const Text('Workspace Agent')),body:Column(children:[Expanded(child:messages.isEmpty?const Center(child:Text('Ask about your Google Workspace')):ListView.builder(padding:const EdgeInsets.all(12),itemCount:messages.length,itemBuilder:(context,i){final m=messages[i];return Align(alignment:m.user?Alignment.centerRight:Alignment.centerLeft,child:Card(color:m.user?Theme.of(context).colorScheme.primaryContainer:null,child:Padding(padding:const EdgeInsets.all(12),child:Column(crossAxisAlignment:CrossAxisAlignment.start,children:[MarkdownBody(data:m.text.isEmpty?'…':m.text),if(!m.user&&!streaming)Row(mainAxisSize:MainAxisSize.min,children:[IconButton(onPressed:()=>service.sendFeedback(session,1),icon:const Icon(Icons.thumb_up_outlined)),IconButton(onPressed:()=>service.sendFeedback(session,-1),icon:const Icon(Icons.thumb_down_outlined))])]))));})),if(error!=null)Text(error!,style:TextStyle(color:Theme.of(context).colorScheme.error)),SafeArea(child:Padding(padding:const EdgeInsets.all(8),child:Row(children:[Expanded(child:TextField(controller:input,onSubmitted:(_)=>send(),decoration:const InputDecoration(hintText:'Type a message…',border:OutlineInputBorder()))),IconButton(onPressed:streaming?null:send,icon:const Icon(Icons.send))]))) ]),bottomNavigationBar:NavigationBar(selectedIndex:0,destinations:const [NavigationDestination(icon:Icon(Icons.chat),label:'Chat'),NavigationDestination(icon:Icon(Icons.history),label:'History'),NavigationDestination(icon:Icon(Icons.settings),label:'Settings')]));
}
